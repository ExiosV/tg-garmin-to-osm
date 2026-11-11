"""Prometheus-style metrics on the ingest port (/metrics, no auth — counters and gauges only,
no user data). Hand-rolled: no extra dependency, the surface is tiny.

Counters live in-process; gauges (activity statuses, user counts, DB size, resource usage) are
computed at scrape time from the DB and /proc, so a restart never loses history except the
in-process counters themselves.
"""
import logging
import os
import resource
import sqlite3
import threading
import time

log = logging.getLogger("tg_garmin_to_osm")


class Metrics:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._counters: dict[str, float] = {}
        self._labels: dict[str, dict[str, str]] = {}

    def inc(self, name: str, value: float = 1.0, **labels: str) -> None:
        label_key = _labels_key(labels)
        with self._lock:
            full = f"{name}{label_key}"
            self._counters[full] = self._counters.get(full, 0.0) + value
            if labels:
                self._labels[name] = dict(labels)

    def render(self, data_dir: str, cfg) -> str:
        lines = []
        with self._lock:
            for full, value in sorted(self._counters.items()):
                lines.append(f"{full} {value}")
        for name, value in _gauge_lines(data_dir, cfg):
            lines.append(f"{name} {value}")
        return "\n".join(lines) + "\n"


def _labels_key(labels: dict) -> str:
    if not labels:
        return ""
    inner = ",".join(f'{k}="{v}"' for k, v in sorted(labels.items()))
    return "{" + inner + "}"


def _read_db_gauges(data_dir: str) -> list:
    """Activity statuses per user-kind and user counts, read on a separate short-lived connection."""
    out = []
    try:
        db = sqlite3.connect(os.path.join(data_dir, "state.db"), timeout=10)
        db.row_factory = sqlite3.Row
        rows = db.execute(
            "SELECT u.channel, a.status, COUNT(*) AS n FROM activities a "
            "JOIN users u ON u.user_id = a.user_id GROUP BY u.channel, a.status"
        ).fetchall()
        for r in rows:
            out.append(('activities_by_status{channel="%s",status="%s"}' % (r["channel"] or "none", r["status"]),
                        r["n"]))
        events = db.execute("SELECT kind, COUNT(*) AS n FROM events GROUP BY kind").fetchall()
        for r in events:
            out.append((f'events_total{{kind="{r["kind"]}"}}', r["n"]))
        users = db.execute(
            "SELECT channel, COUNT(*) AS n, SUM(CASE WHEN garmin_linked=1 THEN 1 ELSE 0 END), "
            "SUM(CASE WHEN osm_access_token IS NOT NULL THEN 1 ELSE 0 END), "
            "SUM(CASE WHEN auto=1 THEN 1 ELSE 0 END) FROM users GROUP BY channel"
        ).fetchall()
        for r in users:
            ch = r["channel"] or "none"
            out.append((f'users_total{{channel="{ch}"}}', r["n"]))
            out.append((f'users_garmin_linked{{channel="{ch}"}}', r[2] or 0))
            out.append((f'users_osm_linked{{channel="{ch}"}}', r[3] or 0))
            out.append((f'users_auto_on{{channel="{ch}"}}', r[4] or 0))
        db.close()
    except Exception as e:  # metrics must never break the process
        log.warning("metrics db gauges failed: %s", e)
    return out


def _gauge_lines(data_dir: str, cfg) -> list:
    lines = []
    try:
        db_path = os.path.join(data_dir, "state.db")
        if os.path.exists(db_path):
            lines.append(("db_size_bytes", os.path.getsize(db_path)))
        rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * 1024  # KiB -> bytes on Linux
        lines.append(("process_rss_bytes", rss))
        utime, stime = os.times()[:2]
        lines.append(("process_cpu_seconds_total", utime + stime))
        lines.append(("process_uptime_seconds", time.time() - _START))
        lines.append(("limit_storage_bytes", 100 * 1024 * 1024))  # PVC quota (app.yaml)
        lines.append(("limit_memory_bytes", 256 * 1024 * 1024))   # container memory limit (app.yaml)
        lines.append(("limit_web_users", cfg.max_web_users))
        lines.append(("config_garmin_poll_seconds", cfg.garmin_poll_seconds))
        lines.append(("config_scan_page", cfg.scan_page))
        for name, value in _read_db_gauges(data_dir):
            lines.append((name, value))
    except Exception as e:  # metrics must never break the process
        log.warning("metrics gauges failed: %s", e)
    return [(name, f"{value}") for name, value in lines]


_START = time.time()

METRICS = Metrics()  # process-wide registry (the ingest server and the runner share it)
