"""Subscription quota usage (5-hour and weekly windows) around an edition.

Claude Code reports the plan's utilization in a `rate_limit_event` of its
stream-json output. A trivial Haiku call before and after writing gives two
snapshots; their difference is the edition's share of each window. It is an
approximation: utilization has a 1-point resolution, and any other Claude
usage during the run (another session…) is counted too.
"""

from __future__ import annotations

import json
import logging
import subprocess
from typing import Any

log = logging.getLogger("daily_paper")
WINDOWS = ("five_hour", "seven_day")


def snapshot() -> dict[str, Any] | None:
    """{window: {"utilization": 0..1, "resetsAt": epoch}} or None if unavailable."""
    argv = ["claude", "-p", "ok", "--model", "haiku", "--tools", "", "--strict-mcp-config",
            "--setting-sources", "", "--no-session-persistence", "--output-format", "stream-json", "--verbose"]
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=120)
    except (OSError, subprocess.TimeoutExpired) as exc:
        log.warning("quota indisponible : %s", exc)
        return None
    windows = None
    for line in proc.stdout.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if event.get("type") == "rate_limit_event":
            windows = (event.get("rate_limit_info") or {}).get("unifiedWindows") or windows
    if not windows:
        log.warning("quota indisponible : pas de rate_limit_event (connexion par clé API ?)")
        return None
    return {w: windows[w] for w in WINDOWS if w in windows}


def usage(before: dict[str, Any] | None, after: dict[str, Any] | None) -> dict[str, Any] | None:
    """Per window: levels before/after in % and the edition's share in points."""
    if not before or not after:
        return None
    out = {}
    for w in WINDOWS:
        if w not in before or w not in after:
            continue
        b, a = before[w]["utilization"] * 100, after[w]["utilization"] * 100
        reset = before[w].get("resetsAt") != after[w].get("resetsAt")
        # Window reset mid-run: only the usage since the reset is visible.
        out[w] = {"before": round(b), "after": round(a), "points": round(a if reset else max(a - b, 0)), "reset": reset}
    return out or None
