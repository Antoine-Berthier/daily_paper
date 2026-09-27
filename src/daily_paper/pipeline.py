"""One edition, end to end: gate → feedback → plan → write → validate → render.

Only this module mutates `editions/` and appends to the history; the planner's
state changes (credits, bags, recency) are committed at the very end, so a run
that fails part-way consumes nothing.
"""

from __future__ import annotations

import json
import logging
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta
from typing import Any

from . import config, notify, planner, render, state, validate, web, writer
from .planner import ArticlePlan

log = logging.getLogger("daily_paper")


def gate(st: dict[str, Any], settings: dict[str, Any], now: datetime, *, auto: bool) -> str | None:
    """Why no edition should be produced now (None → go)."""
    today = now.date()
    if auto and today.weekday() not in settings["schedule"]["weekdays"]:
        return "pas un jour de parution"
    if auto and any(e["date"] == today.isoformat() for e in st["editions"].values()):
        return "édition du jour déjà produite"
    horizon = (today - timedelta(days=int(settings["unread_block_days"]))).isoformat()
    unread = sorted(e["date"] for e in st["editions"].values() if not e.get("read_at") and e["date"] > horizon)
    if unread:
        return f"l'édition du {unread[-1]} n'est pas encore lue"
    return None


def _edition_id(st: dict[str, Any], today: date) -> str:
    base, n = today.isoformat(), 2
    eid = base
    while eid in st["editions"]:
        eid, n = f"{base}-{n}", n + 1
    return eid


def _verbatim(art: ArticlePlan) -> dict[str, Any] | None:
    """An article made of the drawn entry as is (topics with `writer: none`)."""
    exp = art.seed.get("expression")
    if not exp:
        log.warning("[%s] aucune entrée tirée (dico2rue injoignable ?)", art.topic)
        return None
    return {
        "kind": "verbatim", "title": exp["expression"], "definition": exp["definition"], "example": exp["example"],
        "sources": [{"title": "dico2rue", "url": exp["url"]}], "subject": exp["expression"],
        "summary": exp["definition"][:200], "entities": [],
    }


def _write_one(
    art: ArticlePlan, plan: planner.EditionPlan, settings: dict[str, Any], history: list[dict[str, Any]], today: date
) -> tuple[dict[str, Any] | None, float]:
    if art.spec.get("writer") == "none":
        return _verbatim(art), 0.0
    target = int(art.spec.get("words") or settings["words"])
    note, cost = None, 0.0
    for attempt in range(int(settings["writer"]["retries"]) + 1):
        prompt = writer.build_prompt(art, settings, history, plan.articles, today, feedback_note=note)
        try:
            out, c = writer.call_claude(prompt, settings)
        except writer.WriterError as exc:
            log.warning("[%s] échec de rédaction (essai %d) : %s", art.topic, attempt + 1, exc)
            note = None
            continue
        cost += c
        if out.get("skip"):
            if art.spec.get("optional"):
                log.info("[%s] rubrique sautée : %s", art.topic, out.get("skip_reason", ""))
                return None, cost
            note = "Cette rubrique n'est pas optionnelle : écris l'article (skip = false)."
            continue
        out, problems = validate.check(out, art.topic, target, history)
        if not problems:
            log.info("[%s] ok — %s (%.2f $)", art.topic, out["title"], cost)
            return out, cost
        note = "\n".join(f"- {p}" for p in problems)
        log.warning("[%s] rejet (essai %d) :\n%s", art.topic, attempt + 1, note)
    return None, cost


def _attach_image(article: dict[str, Any], folder, art_id: str) -> None:
    img = article.get("image") or {}
    candidates: list[tuple[str | None, str | None]] = []  # (image url, page it comes from)
    if img.get("image_url"):
        candidates.append((img["image_url"], img.get("page_url")))
    pages = [img.get("page_url")] + [s["url"] for s in article.get("sources", [])]
    for page in dict.fromkeys(p for p in pages if p):
        candidates.append((None, page))
    for url, page in candidates[:4]:
        url = url or (web.page_image(page) if page else None)
        got = web.download_image(url) if url else None
        if got:
            data, ext = got
            (folder / "img").mkdir(exist_ok=True)
            (folder / "img" / f"{art_id}{ext}").write_bytes(data)
            article["image_file"] = f"img/{art_id}{ext}"
            article["image_source"] = page or url
            article.setdefault("image", {})
            return


def run(*, force: bool = False, only: list[str] | None = None, auto: bool = False, send_notification: bool = True) -> str | None:
    """Produce an edition; return its id, or None when gated or empty."""
    from . import feedback  # local import: feedback imports pipeline helpers indirectly

    settings = config.settings()
    now = datetime.now()
    today = now.date()
    if not force:
        reason = gate(state.load(), settings, now, auto=auto)
        if reason:
            log.info("pas d'édition : %s", reason)
            return None

    try:
        feedback.process_pending()
        settings = config.settings()
    except Exception:
        log.exception("traitement des retours en échec — on continue avec la config actuelle")

    history = state.history()
    plan = planner.plan(state.load(), history, only=only, today=today)
    log.info("plan : %s", ", ".join(f"{a.topic}{'/' + a.subtype if a.subtype else ''}" for a in plan.articles))
    for a in plan.articles:
        log.info("  %s → %s", a.topic, json.dumps(a.seed, ensure_ascii=False)[:300])

    with ThreadPoolExecutor(int(settings["writer"]["parallel"])) as pool:
        results = list(pool.map(lambda a: _write_one(a, plan, settings, history, today), plan.articles))
    total_cost = sum(c for _, c in results)
    written = [(a, out) for a, (out, _) in zip(plan.articles, results) if out]
    if not written:
        log.error("aucun article produit (%.2f $ dépensés)", total_cost)
        return None

    with state.transaction() as st:
        eid = _edition_id(st, today)
        number = len(st["editions"]) + 1
        st["editions"][eid] = {"date": today.isoformat(), "created_at": state.now_iso(), "read_at": None,
                               "number": number, "topics": [a.topic for a, _ in written], "cost_usd": round(total_cost, 3)}
        for key in ("credits", "bags", "last_used"):
            st[key] = plan.state[key]

    folder = config.EDITIONS_DIR / eid
    folder.mkdir(parents=True, exist_ok=True)
    articles = []
    for art, out in written:
        if out.get("kind") != "verbatim":
            _attach_image(out, folder, art.id)
        articles.append({**out, "id": art.id, "topic": art.topic, "subtype": art.subtype,
                         "label": art.spec.get("label", art.topic), "seed": art.seed})
    edition = {"id": eid, "name": settings.get("name", "Daily Paper"), "date": today.isoformat(),
               "date_label": writer.date_fr(today).capitalize(), "number": number,
               "articles": articles, "cost_usd": round(total_cost, 3)}
    (folder / "edition.json").write_text(json.dumps(edition, ensure_ascii=False, indent=1), encoding="utf-8")
    (folder / "index.html").write_text(render.render_edition(edition), encoding="utf-8")

    state.append_history([{
        "edition": eid, "date": today.isoformat(), "article": a["id"], "topic": a["topic"], "subtype": a["subtype"],
        "title": a["title"], "subject": a.get("subject", ""), "summary": a.get("summary", ""),
        "entities": a.get("entities", []), "urls": [s["url"] for s in a.get("sources", [])],
        "seed": a["seed"], "continuity_of": a.get("continuity_of") or None,
    } for a in articles])
    log.info("édition %s publiée : %d articles, %.2f $", eid, len(articles), total_cost)
    if send_notification:
        notify.edition_ready(eid, edition["date_label"], [a["title"] for a in articles], settings)
    return eid


def rerender_all() -> int:
    """Re-render every edition from its edition.json (after a template change)."""
    n = 0
    for path in sorted(config.EDITIONS_DIR.glob("*/edition.json")):
        edition = json.loads(path.read_text(encoding="utf-8"))
        (path.parent / "index.html").write_text(render.render_edition(edition), encoding="utf-8")
        n += 1
    return n
