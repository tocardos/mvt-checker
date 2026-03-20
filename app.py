"""
app.py — MVT Checker Flask application
Portable iOS forensics tool for Raspberry Pi
"""
import io
import json
import os
import shutil
import threading
from datetime import datetime
from pathlib import Path

import qrcode
from flask import (Flask, Response, abort, jsonify,
                   redirect, render_template, request,
                   send_file, url_for)

import db
import ios_tools
import mvt_runner
from jobs import JobStatus, create_job, get_job, active_jobs

# ── config ────────────────────────────────────────────────────────────────────

SSD_ROOT     = Path("/media/usb0/data")
BACKUP_ROOT  = SSD_ROOT / "mvt-backups"
SYSD_ROOT    = SSD_ROOT / "sysdiagnose"
MVT_OUT_ROOT = SSD_ROOT / "mvt-output"

# Wi-Fi AP credentials — reads from env or falls back to PTS defaults
AP_SSID     = os.environ.get("AP_SSID", "PiRogueNet")
AP_PASS     = os.environ.get("AP_PASS", "")
APP_HOST    = os.environ.get("APP_HOST", "mvt.local")
APP_PORT    = int(os.environ.get("APP_PORT", "5005"))

# Backup password — never put on CLI
BACKUP_PASSWORD = os.environ.get("MVT_BACKUP_PASSWORD", "")

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "mvt-checker-secret")

# ── startup ───────────────────────────────────────────────────────────────────

@app.before_request
def _init():
    db.init_db()
    for d in (BACKUP_ROOT, SYSD_ROOT, MVT_OUT_ROOT):
        d.mkdir(parents=True, exist_ok=True)
    app.before_request_funcs[None].remove(_init)


# ── QR code ───────────────────────────────────────────────────────────────────

@app.route("/qr.png")
def qr_png():
    """
    Generate a QR code that:
    1. Connects the phone to the AP
    2. Opens the app URL
    Encodes as a WiFi+URL combo understood by iOS/Android camera apps.
    """
    wifi_str = f"WIFI:T:WPA;S:{AP_SSID};P:{AP_PASS};;"
    url      = f"http://{APP_HOST}:{APP_PORT}/"
    content  = f"{wifi_str}\n{url}"

    qr = qrcode.QRCode(
        version=None,
        error_correction=qrcode.constants.ERROR_CORRECT_M,
        box_size=8,
        border=4,
    )
    qr.add_data(content)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white")
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    return send_file(buf, mimetype="image/png")


@app.route("/analyse")
def analyse_custom():
    """Standalone MVT analysis page — no device required, user provides backup path."""
    return render_template("analyse.html")


# ── main page ─────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    devices = ios_tools.list_devices()
    return render_template("index.html",
                           devices=devices,
                           ap_ssid=AP_SSID,
                           ap_host=APP_HOST,
                           ap_port=APP_PORT)


# ── device info + wizard ──────────────────────────────────────────────────────

@app.route("/device/<udid>")
def device_view(udid):
    info       = ios_tools.get_device_info(udid)
    db_record  = db.get_device(udid)
    checks     = db.get_device_checks(udid)
    paired, _  = ios_tools.check_pair_status(udid)

    if info and "error" not in info:
        db.upsert_device(info)

    return render_template("device.html",
                           udid=udid,
                           info=info,
                           db_record=db_record,
                           checks=checks,
                           paired=paired,
                           backup_password=bool(BACKUP_PASSWORD))


# ── pairing ───────────────────────────────────────────────────────────────────

@app.route("/api/pair/<udid>", methods=["POST"])
def api_pair(udid):
    ok, msg = ios_tools.pair_device(udid)
    return jsonify({"ok": ok, "msg": msg})


# ── backup ────────────────────────────────────────────────────────────────────

@app.route("/api/backup/<udid>", methods=["POST"])
def api_backup(udid):
    password = request.json.get("password", "") or BACKUP_PASSWORD
    job = create_job("backup", udid)

    def _run():
        job.status = JobStatus.RUNNING
        ok, backup_path = ios_tools.run_backup(
            udid, BACKUP_ROOT, password, job
        )
        job.finish(
            JobStatus.DONE if ok else JobStatus.ERROR,
            result={"backup_path": str(backup_path), "ok": ok}
        )

    threading.Thread(target=_run, daemon=True).start()
    return jsonify({"job_id": job.id})


# ── MVT analysis ──────────────────────────────────────────────────────────────

@app.route("/api/mvt/<udid>", methods=["POST"])
def api_mvt(udid):
    data     = request.json or {}
    password = data.get("password", "") or BACKUP_PASSWORD

    # allow custom backup path for external backups
    custom_path = data.get("backup_path", "").strip()
    if custom_path:
        backup_path = Path(custom_path)
    else:
        backup_path = BACKUP_ROOT / udid

    if not backup_path.exists():
        return jsonify({"error": f"Backup not found at {backup_path}"}), 400

    timestamp  = datetime.now().strftime("%Y%m%d_%H%M%S")
    output_dir = MVT_OUT_ROOT / udid / timestamp

    # Only record DB check if we have a real device udid (not a placeholder)
    check_id = None
    if udid != "custom":
        check_id = db.record_check_start(udid, str(backup_path))

    job = create_job("mvt", udid)
    job.result["check_id"]   = check_id
    job.result["output_dir"] = str(output_dir)

    def _run():
        job.status = JobStatus.RUNNING
        result = mvt_runner.run_mvt_check(
            udid, backup_path, output_dir, password, job
        )
        if check_id is not None:
            db.record_check_finish(
                check_id, udid,
                result["result"],
                result["iocs_found"],
                result["ioc_modules"],
                result.get("mvt_log", ""),
            )
        job.result.update(result)
        job.finish(JobStatus.DONE, result)

    threading.Thread(target=_run, daemon=True).start()
    return jsonify({"job_id": job.id,
                    "output_dir": str(output_dir)})


# ── IOC update ────────────────────────────────────────────────────────────────

@app.route("/api/ioc-update", methods=["POST"])
def api_ioc_update():
    job = create_job("iocupdate")

    def _run():
        job.status = JobStatus.RUNNING
        ok = mvt_runner.update_iocs(job)
        job.finish(JobStatus.DONE if ok else JobStatus.ERROR)

    threading.Thread(target=_run, daemon=True).start()
    return jsonify({"job_id": job.id})


# ── sysdiagnose ───────────────────────────────────────────────────────────────

@app.route("/api/sysdiagnose/<udid>", methods=["POST"])
def api_sysdiagnose(udid):
    timestamp  = datetime.now().strftime("%Y%m%d_%H%M%S")
    output_dir = SYSD_ROOT / udid / timestamp
    job        = create_job("sysdiagnose", udid)

    def _run():
        job.status = JobStatus.RUNNING
        ok, zip_path = ios_tools.run_sysdiagnose(udid, output_dir, job)
        job.finish(
            JobStatus.DONE if ok else JobStatus.ERROR,
            result={"zip_path": str(zip_path) if ok else ""}
        )

    threading.Thread(target=_run, daemon=True).start()
    return jsonify({"job_id": job.id})


# ── SSE log stream ────────────────────────────────────────────────────────────

@app.route("/stream/<job_id>")
def stream(job_id):
    job = get_job(job_id)
    if not job:
        abort(404)

    def _generate():
        yield "retry: 1000\n\n"
        yield from job.iter_sse()

    return Response(
        _generate(),
        mimetype="text/event-stream",
        headers={
            "Cache-Control":   "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


# ── job status polling ────────────────────────────────────────────────────────

@app.route("/api/job/<job_id>")
def job_status(job_id):
    job = get_job(job_id)
    if not job:
        abort(404)
    return jsonify({
        "id":       job.id,
        "type":     job.type,
        "status":   job.status,
        "progress": job.progress,
        "result":   job.result,
    })


# ── results viewer ────────────────────────────────────────────────────────────

@app.route("/results/<udid>/<path:output_dir>")
def results_view(udid, output_dir):
    out_path  = Path("/" + output_dir)
    result    = mvt_runner.parse_mvt_output(out_path)
    timeline  = mvt_runner.load_timeline(out_path)
    db_record = db.get_device(udid)
    return render_template("results.html",
                           udid=udid,
                           result=result,
                           timeline=timeline[:500],
                           timeline_total=len(timeline),
                           db_record=db_record)


# ── download sysdiagnose zip ──────────────────────────────────────────────────

@app.route("/download/sysdiagnose/<udid>")
def download_sysdiagnose(udid):
    # Find most recent zip for this udid under SYSD_ROOT/<udid>/
    udid_dir = SYSD_ROOT / udid
    zips = sorted(udid_dir.glob("sysdiagnose_*.zip"), reverse=True) if udid_dir.exists() else []
    if not zips:
        abort(404)
    return send_file(str(zips[0]),
                     as_attachment=True,
                     download_name=zips[0].name)


# ── delete backup + MVT output (NOT the DB) ───────────────────────────────────

@app.route("/api/delete/<udid>", methods=["POST"])
def api_delete(udid):
    deleted = []
    for root in (BACKUP_ROOT, MVT_OUT_ROOT, SYSD_ROOT):
        target = root / udid
        if target.exists():
            shutil.rmtree(str(target))
            deleted.append(str(target))
    return jsonify({"deleted": deleted,
                    "note": "Device history in DB was kept."})


# ── device history ────────────────────────────────────────────────────────────

@app.route("/history")
def history():
    devices = db.get_all_devices()
    return render_template("history.html", devices=devices)


@app.route("/history/<udid>")
def device_history(udid):
    device = db.get_device(udid)
    checks = db.get_device_checks(udid)
    if not device:
        abort(404)
    return render_template("device_history.html",
                           device=device,
                           checks=checks)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=APP_PORT, debug=False, threaded=True)
