"""
jobs.py — in-memory job registry + SSE log queue
Each job has a unique ID, a status, and a queue of log lines.
Flask routes read from the queue and stream via SSE.
"""
import uuid
import queue
import threading
from datetime import datetime
from enum import Enum


class JobStatus(str, Enum):
    PENDING   = "pending"
    RUNNING   = "running"
    DONE      = "done"
    ERROR     = "error"
    CANCELLED = "cancelled"


class Job:
    def __init__(self, job_type: str, udid: str = ""):
        self.id        = str(uuid.uuid4())[:8]
        self.type      = job_type          # "backup" | "mvt" | "sysdiagnose" | "iocupdate"
        self.udid      = udid
        self.status    = JobStatus.PENDING
        self.created   = datetime.now()
        self.progress  = 0                 # 0-100 int for backup progress bar
        self.result    = {}                # populated on completion
        self._q        = queue.Queue()
        self._done_evt = threading.Event()

    # ── log helpers ──────────────────────────────────────────────────────────

    def log(self, line: str, level: str = "info"):
        """Push a log line into the SSE queue."""
        self._q.put({"level": level, "msg": line.rstrip()})

    def log_ok(self, line: str):   self.log(line, "ok")
    def log_err(self, line: str):  self.log(line, "error")
    def log_warn(self, line: str): self.log(line, "warn")

    def set_progress(self, pct: int):
        self.progress = max(0, min(100, pct))
        self._q.put({"level": "progress", "msg": str(self.progress)})

    def finish(self, status: JobStatus = JobStatus.DONE, result: dict = None):
        self.status = status
        if result:
            self.result = result
        self._q.put(None)          # sentinel → SSE generator closes
        self._done_evt.set()

    def iter_sse(self):
        """
        Generator consumed by the /stream/<job_id> route.
        Yields Server-Sent Event formatted strings.
        """
        while True:
            item = self._q.get()
            if item is None:
                yield "event: done\ndata: {}\n\n"
                break
            level = item.get("level", "info")
            msg   = item.get("msg", "").replace("\n", "\\n")
            yield f"event: {level}\ndata: {msg}\n\n"

    def wait(self):
        self._done_evt.wait()


# ── global registry ───────────────────────────────────────────────────────────

_registry: dict[str, Job] = {}
_lock = threading.Lock()


def create_job(job_type: str, udid: str = "") -> Job:
    job = Job(job_type, udid)
    with _lock:
        _registry[job.id] = job
    return job


def get_job(job_id: str) -> Job | None:
    return _registry.get(job_id)


def active_jobs() -> list[Job]:
    with _lock:
        return [j for j in _registry.values()
                if j.status in (JobStatus.PENDING, JobStatus.RUNNING)]
