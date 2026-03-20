"""
db.py — SQLite device history
Stored on the SSD so it persists across backup wipes.
Never deleted when backup data is cleaned up.
"""
import sqlite3
import json
from datetime import datetime
from pathlib import Path

DB_PATH = Path("/media/django/data/mvt-checker.db")


def _conn():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(DB_PATH))
    con.row_factory = sqlite3.Row
    return con


def init_db():
    with _conn() as con:
        con.executescript("""
        CREATE TABLE IF NOT EXISTS devices (
            udid          TEXT PRIMARY KEY,
            serial        TEXT,
            model         TEXT,
            product_type  TEXT,
            ios_version   TEXT,
            device_name   TEXT,
            first_seen    TEXT,
            last_seen     TEXT,
            check_count   INTEGER DEFAULT 0,
            last_result   TEXT DEFAULT 'unknown',
            iocs_found    INTEGER DEFAULT 0,
            ioc_modules   TEXT DEFAULT '[]',
            notes         TEXT DEFAULT ''
        );

        CREATE TABLE IF NOT EXISTS checks (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            udid          TEXT NOT NULL,
            started_at    TEXT NOT NULL,
            finished_at   TEXT,
            backup_path   TEXT,
            mvt_output    TEXT,
            result        TEXT DEFAULT 'unknown',
            iocs_found    INTEGER DEFAULT 0,
            ioc_modules   TEXT DEFAULT '[]',
            FOREIGN KEY(udid) REFERENCES devices(udid)
        );
        """)


# ── device upsert ─────────────────────────────────────────────────────────────

def upsert_device(info: dict):
    """
    info keys: udid, serial, model, product_type, ios_version, device_name
    Creates or updates the device row, bumping last_seen.
    """
    now = datetime.now().isoformat()
    with _conn() as con:
        existing = con.execute(
            "SELECT udid FROM devices WHERE udid=?", (info["udid"],)
        ).fetchone()
        if existing:
            con.execute("""
                UPDATE devices SET
                    serial=?, model=?, product_type=?, ios_version=?,
                    device_name=?, last_seen=?
                WHERE udid=?
            """, (
                info.get("serial", ""),
                info.get("model", ""),
                info.get("product_type", ""),
                info.get("ios_version", ""),
                info.get("device_name", ""),
                now,
                info["udid"],
            ))
        else:
            con.execute("""
                INSERT INTO devices
                    (udid, serial, model, product_type, ios_version,
                     device_name, first_seen, last_seen)
                VALUES (?,?,?,?,?,?,?,?)
            """, (
                info["udid"],
                info.get("serial", ""),
                info.get("model", ""),
                info.get("product_type", ""),
                info.get("ios_version", ""),
                info.get("device_name", ""),
                now, now,
            ))


def record_check_start(udid: str, backup_path: str) -> int:
    now = datetime.now().isoformat()
    with _conn() as con:
        cur = con.execute("""
            INSERT INTO checks (udid, started_at, backup_path)
            VALUES (?,?,?)
        """, (udid, now, backup_path))
        return cur.lastrowid


def record_check_finish(check_id: int, udid: str,
                        result: str, iocs_found: int,
                        ioc_modules: list, mvt_output: str):
    now = datetime.now().isoformat()
    modules_json = json.dumps(ioc_modules)
    with _conn() as con:
        con.execute("""
            UPDATE checks SET
                finished_at=?, result=?, iocs_found=?,
                ioc_modules=?, mvt_output=?
            WHERE id=?
        """, (now, result, iocs_found, modules_json, mvt_output, check_id))
        con.execute("""
            UPDATE devices SET
                last_seen=?, last_result=?, iocs_found=?,
                ioc_modules=?, check_count=check_count+1
            WHERE udid=?
        """, (now, result, iocs_found, modules_json, udid))


# ── queries ───────────────────────────────────────────────────────────────────

def get_device(udid: str) -> dict | None:
    with _conn() as con:
        row = con.execute(
            "SELECT * FROM devices WHERE udid=?", (udid,)
        ).fetchone()
        return dict(row) if row else None


def get_all_devices() -> list[dict]:
    with _conn() as con:
        rows = con.execute(
            "SELECT * FROM devices ORDER BY last_seen DESC"
        ).fetchall()
        return [dict(r) for r in rows]


def get_device_checks(udid: str) -> list[dict]:
    with _conn() as con:
        rows = con.execute(
            "SELECT * FROM checks WHERE udid=? ORDER BY started_at DESC",
            (udid,)
        ).fetchall()
        return [dict(r) for r in rows]
