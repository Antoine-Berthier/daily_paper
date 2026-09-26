"""Runtime state: scheduling credits, diversity bags, reads, history, feedback.

`state.json` is small and rewritten whole under an exclusive file lock, so the
server, its scheduler thread and a CLI run can share it safely. History and
feedback are append-only JSONL files.
"""

from __future__ import annotations

import fcntl
import json
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any

from .config import DATA_DIR

STATE_FILE = DATA_DIR / "state.json"
HISTORY_FILE = DATA_DIR / "history.jsonl"
FEEDBACK_FILE = DATA_DIR / "feedback.jsonl"
LOCK_FILE = DATA_DIR / ".lock"

EMPTY: dict[str, Any] = {
    "credits": {},      # topic → scheduling credit
    "bags": {},         # bag key → values left before the next reshuffle
    "last_used": {},    # recency key → {value: ISO date}
    "bandit": {},       # topic[/subtype] → [alpha, beta]
    "editions": {},     # edition id → metadata (date, read_at, topics…)
    "last_auto_attempt": None,
    "feedback_offset": 0,  # feedback.jsonl lines already processed
}


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


@contextmanager
def _locked() -> Iterator[None]:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with LOCK_FILE.open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def _load_unlocked() -> dict[str, Any]:
    if not STATE_FILE.exists():
        return json.loads(json.dumps(EMPTY))
    data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    return {**json.loads(json.dumps(EMPTY)), **data}


def load() -> dict[str, Any]:
    with _locked():
        return _load_unlocked()


@contextmanager
def transaction() -> Iterator[dict[str, Any]]:
    """Read-modify-write the state atomically."""
    with _locked():
        state = _load_unlocked()
        yield state
        tmp = STATE_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(STATE_FILE)


def _append(path: Path, rows: list[dict[str, Any]]) -> None:
    with _locked(), path.open("a", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return rows


def history() -> list[dict[str, Any]]:
    return _read_jsonl(HISTORY_FILE)


def append_history(rows: list[dict[str, Any]]) -> None:
    _append(HISTORY_FILE, rows)


def feedback() -> list[dict[str, Any]]:
    return _read_jsonl(FEEDBACK_FILE)


def append_feedback(row: dict[str, Any]) -> None:
    _append(FEEDBACK_FILE, [{"at": now_iso(), **row}])
