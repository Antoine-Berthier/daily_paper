"""Local web server + daily scheduler, in one long-lived process.

Serves editions, records reads/ratings/comments/clicks, runs on-demand jobs,
and fires the automatic edition once per publishing day (catching up at start
if the machine was off at the scheduled time).
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
from datetime import datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import unquote, urlparse

from . import config, feedback, pipeline, render, state, writer

log = logging.getLogger("daily_paper")
EDITION_ID = re.compile(r"^\d{4}-\d{2}-\d{2}(-\d+)?$")
MIME = {".html": "text/html; charset=utf-8", ".jpg": "image/jpeg", ".png": "image/png", ".webp": "image/webp",
        ".gif": "image/gif", ".avif": "image/avif", ".json": "application/json"}


class Jobs:
    """At most one background job (edition or feedback) at a time."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.current: str | None = None
        self.detail = ""
        self.last_result = ""
        self.last_edition: str | None = None

    def start(self, label: str, fn) -> tuple[bool, str]:
        with self.lock:
            if self.current:
                return False, f"déjà en cours : {self.current}"
            self.current, self.detail = label, ""

        def target() -> None:
            try:
                self.last_result = fn() or ""
            except Exception as exc:  # a job must never kill the server
                log.exception("job %s en échec", label)
                self.last_result = f"échec : {exc}"
            finally:
                with self.lock:
                    self.current = None

        threading.Thread(target=target, daemon=True, name=label).start()
        return True, ""

    def status(self) -> dict[str, Any]:
        job = f"{self.current} — {self.detail}" if self.current and self.detail else self.current
        return {"job": job, "last_result": self.last_result, "last_edition": self.last_edition}


JOBS = Jobs()


class _JobLogHandler(logging.Handler):
    def emit(self, record: logging.LogRecord) -> None:
        if JOBS.current:
            JOBS.detail = record.getMessage().splitlines()[0][:140]


def generate(only: list[str] | None = None, *, force: bool = True, auto: bool = False) -> str:
    eid = pipeline.run(force=force, only=only or None, auto=auto)
    if eid:
        JOBS.last_edition = eid
        return f"édition {eid} prête"
    return "pas d'édition produite (voir data/daily_paper.log)"


def apply_feedback() -> str:
    changes = feedback.process_pending()
    return "retours appliqués : " + ("; ".join(changes) if changes else "rien à changer")


def _editions() -> list[dict[str, Any]]:
    st = state.load()
    topics = config.topics()
    rows = []
    for eid, meta in sorted(st["editions"].items(), reverse=True):
        rows.append({
            "id": eid, "read_at": meta.get("read_at"),
            "date_label": writer.date_fr(datetime.fromisoformat(meta["date"]).date()).capitalize()
            + (f" ({eid.rsplit('-', 1)[1]})" if eid.count("-") == 3 else ""),
            "labels": [topics.get(t, {}).get("label", t) for t in meta.get("topics", [])],
        })
    return rows


class Handler(BaseHTTPRequestHandler):
    server_version = "DailyPaper/0.1"

    def log_message(self, fmt: str, *args: Any) -> None:  # quiet access log
        pass

    # ------------------------------------------------------------ helpers
    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, data: Any, code: int = 200) -> None:
        self._send(code, json.dumps(data, ensure_ascii=False).encode(), "application/json; charset=utf-8")

    def _body(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length") or 0)
        try:
            data = json.loads(self.rfile.read(min(length, 100_000)) or b"{}")
            return data if isinstance(data, dict) else {}
        except json.JSONDecodeError:
            return {}

    # ------------------------------------------------------------ GET
    def do_GET(self) -> None:
        path = unquote(urlparse(self.path).path)
        if path == "/":
            status = {**JOBS.status(), "pending_feedback": len(feedback.pending()),
                      "recent_changes": feedback.recent_config_changes()}
            html = render.render_index(_editions(), config.topics(), status)
            return self._send(200, html.encode(), MIME[".html"])
        if path == "/api/status":
            return self._json({**JOBS.status(), "pending_feedback": len(feedback.pending())})
        m = re.fullmatch(r"/e/([^/]+)(/.*)?", path)
        if m and EDITION_ID.match(m.group(1)):
            eid, rest = m.group(1), m.group(2)
            if rest is None:
                self.send_response(HTTPStatus.MOVED_PERMANENTLY)
                self.send_header("Location", f"/e/{eid}/")
                self.end_headers()
                return
            folder = (config.EDITIONS_DIR / eid).resolve()
            target = (folder / (rest.lstrip("/") or "index.html")).resolve()
            if target.is_file() and folder in target.parents and target.suffix in MIME:
                return self._send(200, target.read_bytes(), MIME[target.suffix])
        self._send(404, b"introuvable", "text/plain; charset=utf-8")

    # ------------------------------------------------------------ POST
    def do_POST(self) -> None:
        path = urlparse(self.path).path
        data = self._body()
        eid = data.get("edition")
        if eid is not None and not (isinstance(eid, str) and EDITION_ID.match(eid)):
            return self._json({"error": "edition invalide"}, 400)
        if path == "/api/read":
            with state.transaction() as st:
                meta = st["editions"].get(eid)
                if meta and not meta.get("read_at"):
                    meta["read_at"] = state.now_iso()
            return self._json({"ok": True})
        if path == "/api/feedback":
            row = {k: data[k] for k in ("edition", "article") if data.get(k)}
            if "rating" in data:
                row["rating"] = max(-1, min(1, int(data["rating"])))
            if str(data.get("text", "")).strip():
                row["text"] = str(data["text"]).strip()[:2000]
            if "rating" not in row and "text" not in row:
                return self._json({"error": "vide"}, 400)
            state.append_feedback(row)
            return self._json({"ok": True})
        if path == "/api/click":
            if data.get("article"):
                state.append_feedback({"edition": eid, "article": data["article"], "click": str(data.get("url", ""))[:500]})
            return self._json({"ok": True})
        if path == "/api/generate":
            topics = [t for t in data.get("topics") or [] if t in config.topics()]
            started, reason = JOBS.start("rédaction de l'édition", lambda: generate(topics))
            return self._json({"started": started, "reason": reason})
        if path == "/api/apply-feedback":
            started, reason = JOBS.start("application des retours", apply_feedback)
            return self._json({"started": started, "reason": reason})
        self._json({"error": "introuvable"}, 404)


def scheduler_loop(stop: threading.Event) -> None:
    while not stop.is_set():
        try:
            settings = config.settings()
            now = datetime.now()
            sched = settings["schedule"]
            due = now.weekday() in sched["weekdays"] and now.strftime("%H:%M") >= sched["time"]
            if due and state.load().get("last_auto_attempt") != now.date().isoformat():
                started, _ = JOBS.start("édition automatique", lambda: generate(force=False, auto=True))
                if started:
                    with state.transaction() as st:
                        st["last_auto_attempt"] = now.date().isoformat()
        except Exception:
            log.exception("planificateur")
        stop.wait(60)


def serve() -> None:
    settings = config.settings()
    host, port = settings["server"]["host"], int(settings["server"]["port"])
    logging.getLogger("daily_paper").addHandler(_JobLogHandler())
    httpd = ThreadingHTTPServer((host, port), Handler)
    stop = threading.Event()
    threading.Thread(target=scheduler_loop, args=(stop,), daemon=True, name="scheduler").start()
    log.info("serveur sur http://%s:%d/ (édition auto %s, jours %s)", host, port,
             settings["schedule"]["time"], settings["schedule"]["weekdays"])
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        httpd.server_close()
        time.sleep(0.1)
