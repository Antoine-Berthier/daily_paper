"""Edition planning: which topics run today, and with which diversity seed.

All randomness lives here, in Python — the writer model only executes a
brief. Mechanisms (see config/topics.yaml for how each topic uses them):

* credit scheduling  — each topic accrues `probability × bandit factor ×
  jitter` per edition and runs once its credit reaches 1 (smooth weighted
  round-robin): long-run frequencies hold without droughts or bursts;
* shuffle bags       — every value of a pool comes out once before any repeats;
* recency weighting  — weight = 1 − exp(−days since last use / τ);
* feed seeds         — a random, never-used recent post from a curated feed;
* providers          — e.g. a random, never-published dico2rue entry;
* verbalized sampling— the model lists candidates with probabilities and must
  take the rank drawn here, in the tail of its own distribution;
* angles             — a random writing angle, so form varies as well as content;
* Thompson bandit    — reader ratings nudge topic weights within [0.6, 1.4].

`plan()` works on a copy of the state; the caller commits `plan.state` only
once the edition is actually published, so a failed run consumes nothing.
"""

from __future__ import annotations

import copy
import math
import random
import uuid
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from . import config, web

RECENCY_TAU_DAYS = 45
CREDIT_BOUNDS = (-1.0, 2.0)
VERBALIZED_CANDIDATES = 8


@dataclass
class ArticlePlan:
    id: str
    topic: str
    subtype: str | None
    spec: dict[str, Any]
    seed: dict[str, Any] = field(default_factory=dict)


@dataclass
class EditionPlan:
    articles: list[ArticlePlan]
    state: dict[str, Any]


# ---------------------------------------------------------------- primitives


def bandit_factor(state: dict[str, Any], key: str, rng: random.Random) -> float:
    """Thompson sample mapped to [0.6, 1.4]; 1.0 on average without feedback."""
    a, b = state["bandit"].get(key, [1.0, 1.0])
    return 0.6 + 0.8 * rng.betavariate(a, b)


def draw_bag(state: dict[str, Any], key: str, values: list[Any], rng: random.Random) -> Any:
    """Draw without replacement; refill (reshuffled) once empty or stale."""
    bag = [v for v in state["bags"].get(key, []) if v in values]
    if not bag:
        bag = list(values)
        rng.shuffle(bag)
    value = bag.pop()
    state["bags"][key] = bag
    return value


def draw_recency(
    state: dict[str, Any], key: str, values: list[Any], rng: random.Random, today: date
) -> Any:
    used = state["last_used"].setdefault(key, {})
    weights = []
    for v in values:
        last = used.get(str(v))
        days = (today - date.fromisoformat(last)).days if last else 10_000
        weights.append(1 - math.exp(-max(days, 0) / RECENCY_TAU_DAYS) + 1e-6)
    value = rng.choices(values, weights)[0]
    used[str(value)] = today.isoformat()
    return value


def _facet_values(facet: dict[str, Any], today: date) -> list[Any]:
    if "values" in facet:
        return list(facet["values"])
    values = config.pool(facet["pool"])
    if isinstance(values, dict):  # month-indexed pool (seasonal)
        values = values.get(today.month) or values.get(str(today.month)) or []
    return list(values)


def draw_facet(
    state: dict[str, Any], topic: str, name: str, facet: dict[str, Any], rng: random.Random, today: date
) -> Any:
    values = _facet_values(facet, today)
    if not values:
        return None
    # Pool-backed bags are shared across topics/subtypes (regions are drawn
    # from the same bag whether the article is about a place or a culture).
    key = f"pool:{facet['pool']}" if "pool" in facet else f"facet:{topic}:{name}"
    strategy = facet.get("strategy", "bag")
    if strategy == "bag":
        return draw_bag(state, key, values, rng)
    if strategy == "recency":
        return draw_recency(state, key, values, rng, today)
    return rng.choice(values)


# ---------------------------------------------------------------- selection


def choose_topics(
    topics: dict[str, Any], settings: dict[str, Any], state: dict[str, Any], rng: random.Random
) -> list[str]:
    """Always-on topics plus magazine topics picked by credit."""
    always = [t for t, spec in topics.items() if spec.get("always")]
    lo, hi = CREDIT_BOUNDS
    credits = state["credits"]
    for name in [n for n in credits if n not in topics]:
        del credits[name]
    for name, spec in topics.items():
        p = float(spec.get("probability") or 0)
        if spec.get("always") or p <= 0:
            continue
        gain = p * bandit_factor(state, name, rng) * rng.uniform(0.75, 1.25)
        credits[name] = min(hi, credits.get(name, 0.0) + gain)
    ranked = sorted(credits, key=lambda n: credits[n] + rng.uniform(0, 1e-3), reverse=True)
    n_min, n_max = settings["articles"]["min"], settings["articles"]["max"]
    chosen = [n for n in ranked if credits[n] >= 1][:n_max]
    for n in ranked:
        if len(chosen) >= n_min:
            break
        if n not in chosen:
            chosen.append(n)
    for n in chosen:
        credits[n] = max(lo, credits[n] - 1)
    return always + chosen


def choose_subtype(topic: str, spec: dict[str, Any], state: dict[str, Any], rng: random.Random) -> str | None:
    subtypes = spec.get("subtypes") or {}
    if not subtypes:
        return None
    weighted = []
    for name, sub in subtypes.items():
        factor = bandit_factor(state, f"{topic}/{name}", rng)
        weighted += [name] * max(1, round(float(sub.get("weight", 1)) * factor * 2))
    return draw_bag(state, f"subtype:{topic}", weighted, rng)


# ---------------------------------------------------------------- seeds


def draw_seed(
    plan: ArticlePlan, state: dict[str, Any], rng: random.Random, today: date, history: list[dict[str, Any]]
) -> dict[str, Any]:
    spec = plan.spec.get("seed") or {}
    seed: dict[str, Any] = {}
    facets = {}
    for name, facet in (spec.get("facets") or {}).items():
        value = draw_facet(state, plan.topic, name, facet, rng, today)
        if value is not None:
            facets[name] = value
    if facets:
        seed["facets"] = facets
    if spec.get("feeds"):
        used = {u for h in history for u in h.get("urls", [])} | {
            h["seed"]["source"]["url"] for h in history if (h.get("seed") or {}).get("source")
        }
        item = web.random_feed_item(spec["feeds"], int(spec.get("feed_max_age_days", 30)), used, rng)
        if item:
            seed["source"] = item
    if spec.get("provider") == "dico2rue":
        used = {h.get("subject") for h in history if h.get("topic") == plan.topic}
        item = web.dico2rue_expression(used, rng)
        if item:
            seed["expression"] = item
    if spec.get("verbalized"):
        seed["verbalized_rank"] = rng.randint(VERBALIZED_CANDIDATES // 2, VERBALIZED_CANDIDATES)
        seed["verbalized_of"] = VERBALIZED_CANDIDATES
    if spec.get("angle"):
        seed["angle"] = draw_bag(state, "pool:angles", config.pool("angles"), rng)
    return seed


# ---------------------------------------------------------------- entry point


def plan(
    state: dict[str, Any],
    history: list[dict[str, Any]],
    *,
    only: list[str] | None = None,
    today: date | None = None,
    rng: random.Random | None = None,
) -> EditionPlan:
    """Plan an edition. `only` forces an explicit topic list (on-demand runs)."""
    rng = rng or random.Random()
    today = today or date.today()
    state = copy.deepcopy(state)
    topics = config.topics()
    settings = config.settings()
    if only:
        unknown = [t for t in only if t not in topics]
        if unknown:
            raise ValueError(f"rubriques inconnues : {', '.join(unknown)} (connues : {', '.join(topics)})")
        chosen = list(only)
    else:
        chosen = choose_topics(topics, settings, state, rng)
    articles = []
    for name in chosen:
        subtype = choose_subtype(name, topics[name], state, rng)
        spec = config.merged_spec(topics[name], subtype)
        art = ArticlePlan(id=uuid.uuid4().hex[:8], topic=name, subtype=subtype, spec=spec)
        art.seed = draw_seed(art, state, rng, today, history)
        articles.append(art)
    return EditionPlan(articles=articles, state=state)
