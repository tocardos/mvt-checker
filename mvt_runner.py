"""
mvt_runner.py — MVT execution + result parsing
Handles IOC update, check-backup run, and JSON/CSV result parsing.
"""
import csv
import json
import subprocess
import threading
from pathlib import Path

from jobs import Job, JobStatus


# ── IOC update ────────────────────────────────────────────────────────────────

def update_iocs(job: Job) -> bool:
    """
    Download the latest public STIX2 IOCs using MVT's own command:
        mvt-ios download-iocs

    MVT downloads indicator files from the mvt-indicators repository and
    stores them in its appdir (~/.local/share/mvt/ on Linux). They are then
    loaded *automatically* by mvt-ios check-backup — no --iocs flag needed.

    We do NOT use wget / a manual URL because:
      - MVT maintains its own curated list of sources (Pegasus, Cytrox, …)
      - The download location is managed by MVT (XDG appdir)
      - This avoids path mismatches between what we download and what MVT loads
    """
    job.log("Downloading latest IOCs via mvt-ios download-iocs …")
    job.log("IOCs will be stored in MVT's appdir and loaded automatically.")

    rc = _stream_cmd(["mvt-ios", "download-iocs"], job, timeout=180)

    if rc == 0:
        job.log_ok("IOCs downloaded successfully.")
        job.log_ok("They will be used automatically on the next MVT check.")
    else:
        job.log_err(
            f"mvt-ios download-iocs exited with code {rc}. "
            "Check network connectivity on the Pi."
        )
    return rc == 0


# ── MVT check-backup ──────────────────────────────────────────────────────────

def run_mvt_check(udid: str,
                  backup_path: Path,
                  output_path: Path,
                  password: str,
                  job: Job) -> dict:
    """
    Run mvt-ios check-backup.

    IOC matching: MVT auto-loads any indicators previously downloaded with
    'mvt-ios download-iocs' from its appdir — no --iocs flag required.
    We log a reminder if no IOCs appear to be loaded.

    Password: passed via --backup-password (MVT decrypts the backup itself).

    Returns a result dict: result, iocs_found, ioc_modules, modules, output_dir.
    """
    output_path.mkdir(parents=True, exist_ok=True)
    job.log(f"Running: mvt-ios check-backup")
    job.log(f"  Backup : {backup_path}")
    job.log(f"  Output : {output_path}")

    cmd = [
        "mvt-ios", "check-backup",
        "--output", str(output_path),
    ]

    if password:
        cmd += ["--backup-password", password]
        job.log("  Password: provided")

    cmd.append(str(backup_path))

    import os
    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            env=os.environ.copy(),
        )
    except FileNotFoundError:
        job.log_err("mvt-ios not found. Is MVT installed in this environment?")
        return {"result": "error", "iocs_found": 0,
                "ioc_modules": [], "modules": []}

    captured_lines = []
    ioc_loaded_seen = False

    def _reader():
        nonlocal ioc_loaded_seen
        for raw in proc.stdout:
            line = raw.rstrip()
            if not line:
                continue
            captured_lines.append(line)

            # MVT uses Python logging; lines look like:
            #   2024-01-01 12:00:00 INFO     [mvt.ios.modules.fs.safari] Running module …
            #   2024-01-01 12:00:00 WARNING  [mvt.ios.cli] …
            #   2024-01-01 12:00:00 CRITICAL [mvt.ios.cli] DETECTED …
            upper = line.upper()

            if "CRITICAL" in upper or "DETECTED" in upper:
                job.log(line, "error")
            elif "WARNING" in upper:
                job.log(line, "warn")
            elif "ERROR" in upper:
                job.log(line, "error")
            else:
                job.log(line, "ok")

            # Note when IOCs are loaded so we can warn if absent
            if "loaded" in line.lower() and "indicator" in line.lower():
                ioc_loaded_seen = True

    t = threading.Thread(target=_reader, daemon=True)
    t.start()
    try:
        proc.wait(timeout=3600)
    except subprocess.TimeoutExpired:
        proc.kill()
        job.log_err("MVT timed out after 1 hour.")
    t.join()

    if not ioc_loaded_seen:
        job.log_warn(
            "No IOC load message seen. Run 'Update IOCs' from the navbar "
            "to download indicators before checking."
        )

    result = parse_mvt_output(output_path)
    result["mvt_log"] = "\n".join(captured_lines)
    return result


# ── result parsing ────────────────────────────────────────────────────────────

def parse_mvt_output(output_path: Path) -> dict:
    """
    Walk output_path for MVT JSON output files.

    MVT naming convention:
      - <module>.json          → all extracted records for that module
      - <module>_detected.json → records that matched an IOC (only created on hit)

    Strategy:
      1. Collect all *_detected.json files — these are definitive IOC hits.
      2. Also collect plain *.json files (for the "all modules" view),
         but do NOT count them as detections.

    Returns dict: result, iocs_found, ioc_modules, modules (full detail).
    """
    if not output_path.exists():
        return {"result": "error", "iocs_found": 0,
                "ioc_modules": [], "modules": []}

    # Index all plain module files first
    plain_files: dict[str, Path] = {}
    detected_files: dict[str, Path] = {}

    for jf in sorted(output_path.glob("*.json")):
        stem = jf.stem
        if stem.endswith("_detected"):
            # e.g. safari_history_detected → base module = safari_history
            base = stem[: -len("_detected")]
            detected_files[base] = jf
        else:
            plain_files[stem] = jf

    # Build module list (all plain files)
    modules = []
    ioc_modules = []
    total_iocs = 0

    all_module_names = sorted(set(plain_files) | set(detected_files))

    for name in all_module_names:
        # Load detected entries (the real IOC hits)
        det_entries = []
        if name in detected_files:
            try:
                data = json.loads(detected_files[name].read_text())
                det_entries = data if isinstance(data, list) else []
            except (json.JSONDecodeError, OSError):
                pass

        # Load all extracted records for context
        all_data = []
        if name in plain_files:
            try:
                all_data = json.loads(plain_files[name].read_text())
                if not isinstance(all_data, list):
                    all_data = []
            except (json.JSONDecodeError, OSError):
                pass

        count = len(det_entries)
        modules.append({
            "name":     name,
            "file":     str(plain_files.get(name, detected_files.get(name, ""))),
            "count":    count,
            "detected": det_entries[:50],
            "all":      all_data,
        })

        if count > 0:
            ioc_modules.append(name)
            total_iocs += count

    result_label = "clean"
    if total_iocs > 0:
        result_label = "detected"
    elif not modules:
        result_label = "error"

    return {
        "result":      result_label,
        "iocs_found":  total_iocs,
        "ioc_modules": ioc_modules,
        "modules":     modules,
        "output_dir":  str(output_path),
    }


def load_timeline(output_path: Path) -> list[dict]:
    """
    Load timeline.csv from MVT output directory.
    Returns list of row dicts (up to 5000 rows).
    """
    csv_path = output_path / "timeline.csv"
    if not csv_path.exists():
        return []
    rows = []
    try:
        with open(csv_path, newline="", encoding="utf-8", errors="replace") as f:
            reader = csv.DictReader(f)
            for i, row in enumerate(reader):
                if i >= 5000:
                    break
                rows.append(dict(row))
    except OSError:
        pass
    return rows


def load_detected_modules(output_path: Path) -> list[dict]:
    """
    Return only modules that had detections, with their entries.
    """
    result = parse_mvt_output(output_path)
    return [m for m in result.get("modules", []) if m["count"] > 0]


# ── helpers ───────────────────────────────────────────────────────────────────

def _stream_cmd(cmd: list[str], job: Job, timeout: int = 300) -> int:
    import os, signal
    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            preexec_fn=os.setsid,
        )
    except FileNotFoundError as e:
        job.log_err(str(e))
        return -1

    def _r():
        for line in proc.stdout:
            job.log(line.rstrip())

    t = threading.Thread(target=_r, daemon=True)
    t.start()
    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
        job.log_err("Timed out.")
    t.join()
    return proc.returncode
