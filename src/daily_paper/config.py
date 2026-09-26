"""Paths and read access to the YAML configuration (settings, topics, pools).

Configuration lives in `config/` and is versioned: every change made by the
feedback agent is a git commit. Runtime state lives in `data/` and generated
editions in `editions/`, both git-ignored.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(os.environ.get("DAILY_PAPER_HOME", Path(__file__).resolve().parents[2]))
CONFIG_DIR = ROOT / "config"
POOLS_DIR = CONFIG_DIR / "pools"
DATA_DIR = ROOT / "data"
EDITIONS_DIR = ROOT / "editions"
SETTINGS_FILE = CONFIG_DIR / "settings.yaml"
TOPICS_FILE = CONFIG_DIR / "topics.yaml"


def _read(path: Path) -> Any:
    with path.open(encoding="utf-8") as f:
        return yaml.safe_load(f)


def settings() -> dict[str, Any]:
    return _read(SETTINGS_FILE)


def topics() -> dict[str, dict[str, Any]]:
    return _read(TOPICS_FILE)["topics"]


def pool(name: str) -> Any:
    """A list of values (or, for `seasonal`, a month → list mapping)."""
    return _read(POOLS_DIR / f"{name}.yaml")


def pool_names() -> list[str]:
    return sorted(p.stem for p in POOLS_DIR.glob("*.yaml"))


def merged_spec(topic: dict[str, Any], subtype: str | None) -> dict[str, Any]:
    """Topic spec with the subtype's fields layered on top.

    `instructions` are concatenated (topic then subtype); `seed` dicts are
    merged key by key, `facets` included; any other subtype field wins.
    """
    spec = {k: v for k, v in topic.items() if k != "subtypes"}
    if not subtype:
        return spec
    sub = (topic.get("subtypes") or {}).get(subtype) or {}
    for key, value in sub.items():
        if key == "instructions":
            spec["instructions"] = "\n".join(x for x in (topic.get("instructions"), value) if x)
        elif key == "seed":
            seed = dict(topic.get("seed") or {})
            for sk, sv in (value or {}).items():
                seed[sk] = {**(seed.get(sk) or {}), **sv} if sk == "facets" else sv
            spec["seed"] = seed
        elif key != "weight":
            spec[key] = value
    return spec
