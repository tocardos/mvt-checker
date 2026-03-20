"""
ios_tools.py — wrappers around libimobiledevice tools
All long-running calls accept a Job and stream output via job.log()
"""
import re
import subprocess
import threading
from pathlib import Path

from jobs import Job, JobStatus

# ── helpers ───────────────────────────────────────────────────────────────────

def _run(cmd: list[str], timeout: int = 10) -> tuple[int, str, str]:
    """Blocking short-lived command. Returns (returncode, stdout, stderr)."""
    try:
        r = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        return r.returncode, r.stdout.strip(), r.stderr.strip()
    except subprocess.TimeoutExpired:
        return -1, "", "timeout"
    except FileNotFoundError as e:
        return -1, "", str(e)


def _stream(cmd: list[str], job: Job,
            env: dict = None,
            progress_re: re.Pattern = None,
            timeout: int = 7200):
    """
    Run cmd as a subprocess, stream every line to job.log().
    Optionally parse progress percentage via progress_re (group 1 = float).
    Blocks until process exits or timeout.
    Returns returncode.
    """
    import os
    import signal

    full_env = None
    if env:
        full_env = os.environ.copy()
        full_env.update(env)

    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            env=full_env,
            preexec_fn=os.setsid,
        )
    except FileNotFoundError as e:
        job.log_err(f"Command not found: {cmd[0]} — {e}")
        return -1

    def _reader():
        for line in proc.stdout:
            line = line.rstrip()
            if not line:
                continue
            # progress parsing
            if progress_re:
                m = progress_re.search(line)
                if m:
                    try:
                        job.set_progress(int(float(m.group(1))))
                    except ValueError:
                        pass
            job.log(line)

    t = threading.Thread(target=_reader, daemon=True)
    t.start()
    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
        job.log_err("Process timed out and was terminated.")
    t.join()
    return proc.returncode


# ── device detection ──────────────────────────────────────────────────────────

def list_devices() -> list[str]:
    """Return list of connected UDIDs."""
    rc, out, _ = _run(["idevice_id", "-l"])
    if rc != 0 or not out:
        return []
    return [u.strip() for u in out.splitlines() if u.strip()]


def get_device_name(udid: str) -> str:
    rc, out, _ = _run(["idevice_id", "-n", udid])
    return out if rc == 0 else ""


# ── trust / pairing ───────────────────────────────────────────────────────────

def pair_device(udid: str) -> tuple[bool, str]:
    """
    Attempt to pair. Returns (success, message).
    User must tap Trust on the device before calling this.
    """
    rc, out, err = _run(["idevicepair", "-u", udid, "pair"], timeout=30)
    msg = out or err
    return rc == 0, msg


def check_pair_status(udid: str) -> tuple[bool, str]:
    rc, out, err = _run(["idevicepair", "-u", udid, "validate"], timeout=10)
    msg = out or err
    return rc == 0, msg


# ── device info ───────────────────────────────────────────────────────────────

# Keys we want to extract from ideviceinfo output
_INFO_KEYS = {
    "UniqueDeviceID":    "udid",
    "SerialNumber":      "serial",
    "ProductType":       "product_type",
    "ProductVersion":    "ios_version",
    "DeviceName":        "device_name",
    "HardwareModel":     "model",
    "CPUArchitecture":   "cpu_arch",
    "TotalDiskCapacity": "disk_capacity",
    "TimeZone":          "timezone",
    "WiFiAddress":       "wifi_mac",
    "BluetoothAddress":  "bt_mac",
    "PhoneNumber":       "phone_number",
    "IMEI":              "imei",
    "MEID":              "meid",
}

def get_device_info(udid: str) -> dict:
    """
    Run ideviceinfo and return a dict of parsed fields.
    Also returns raw output as 'raw'.
    """
    rc, out, err = _run(["ideviceinfo", "-u", udid], timeout=15)
    if rc != 0:
        return {"error": err or "ideviceinfo failed", "raw": ""}

    info = {"raw": out, "udid": udid}
    for line in out.splitlines():
        if ":" not in line:
            continue
        key, _, val = line.partition(":")
        key = key.strip()
        val = val.strip()
        if key in _INFO_KEYS:
            info[_INFO_KEYS[key]] = val

    return info


# ── backup ────────────────────────────────────────────────────────────────────

# idevicebackup2 prints lines like:
#   Backup progress: 23.45% (file 1234 of 5678)
# OR:
#   23.45%
_BACKUP_PROGRESS_RE = re.compile(r"(\d+(?:\.\d+)?)\s*%")


def run_backup(udid: str, backup_root: Path,
               password: str, job: Job) -> tuple[bool, Path]:
    """
    Run idevicebackup2 backup for udid into backup_root/<udid>/.
    Streams output to job. Returns (success, backup_path).

    Password handling:
      idevicebackup2 does NOT support env var password injection.
      We use a wrapper script approach: write password to a temp file
      and pass it via --password flag, then shred the temp file.
      This keeps the password off the process argument list.
    """
    import tempfile, os

    backup_dir = backup_root / udid
    backup_dir.mkdir(parents=True, exist_ok=True)

    job.log(f"Starting backup for UDID {udid}")
    job.log(f"Destination: {backup_dir}")

    cmd = ["idevicebackup2", "-u", udid, "backup", "--full", str(backup_root)]

    tmpfile_path = None
    if password:
        job.log("Backup password provided — stored temporarily for handoff.")
        # Write to a temp file in /dev/shm (RAM, not SSD) for security
        tmp_dir = Path("/dev/shm") if Path("/dev/shm").exists() else Path("/tmp")
        fd, tmpfile_path = tempfile.mkstemp(dir=str(tmp_dir), prefix=".mvtpw_")
        try:
            os.write(fd, password.encode())
        finally:
            os.close(fd)
        cmd += ["--password", tmpfile_path]

    rc = _stream(cmd, job, progress_re=_BACKUP_PROGRESS_RE, timeout=7200)

    # Shred the temp password file immediately
    if tmpfile_path and Path(tmpfile_path).exists():
        try:
            # overwrite with zeros then delete
            with open(tmpfile_path, "wb") as f:
                f.write(b"\x00" * len(password))
            os.unlink(tmpfile_path)
        except OSError:
            pass

    if rc == 0:
        job.log_ok("Backup completed successfully.")
        job.set_progress(100)
        return True, backup_dir
    else:
        job.log_err(f"idevicebackup2 exited with code {rc}")
        return False, backup_dir


# ── sysdiagnose via idevicecrashreport ───────────────────────────────────────

def run_sysdiagnose(udid: str, output_dir: Path, job: Job) -> tuple[bool, Path]:
    """
    Use idevicecrashreport to pull crash logs and sysdiagnose archives.

    idevicecrashreport usage:
        idevicecrashreport [options] <output-dir>
        -u <udid>   target device
        -k          keep (do not delete) crash reports from device after copy
        -e          extract compressed sysdiagnose archives after copying

    Returns (success, zip_path).
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    job.log(f"Pulling crash reports / sysdiagnose for {udid} …")
    job.log(f"Output directory: {output_dir}")

    cmd = [
        "idevicecrashreport",
        "-u", udid,
        "-k",          # keep reports on device (non-destructive)
        "-e",          # extract sysdiagnose .tar.gz archives in place
        str(output_dir),
    ]

    rc = _stream(cmd, job, timeout=900)

    import shutil
    # zip name lives one level up from the timestamped output_dir,
    # alongside it — not inside it
    zip_base = output_dir.parent / f"sysdiagnose_{udid}"
    zip_path = zip_base.with_suffix(".zip")

    if rc == 0:
        job.log_ok("Crash reports pulled successfully.")
        job.log("Zipping output …")
        shutil.make_archive(str(zip_base), "zip", str(output_dir))
        job.log_ok(f"Archive ready: {zip_path}")
        return True, zip_path
    else:
        # rc==1 often just means "nothing new to copy" — still zip what we got
        files = list(output_dir.rglob("*"))
        if files:
            job.log_warn(
                f"idevicecrashreport exited {rc} but {len(files)} file(s) were copied."
            )
            shutil.make_archive(str(zip_base), "zip", str(output_dir))
            job.log_ok(f"Archive ready: {zip_path}")
            return True, zip_path
        job.log_err(f"idevicecrashreport exited {rc} and no files were copied.")
        return False, zip_path
