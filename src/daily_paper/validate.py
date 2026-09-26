"""Post-writing checks: sourcing, link liveness, length and novelty.

`check()` returns the article with dead secondary links dropped, plus a list of
blocking problems. A non-empty list triggers one rewrite with those problems
fed back to the writer; if it still fails, the article is dropped.
"""

from __future__ import annotations

import re
import unicodedata
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from . import web

LINK = re.compile(r"\]\((https?://[^)\s]+)\)")
DUPLICATE_SUBJECT = 0.55
DUPLICATE_ENTITIES = 0.5


def _norm(text: str) -> str:
    text = unicodedata.normalize("NFKD", text.lower())
    return " ".join("".join(c for c in text if c.isalnum() or c.isspace()).split())


def _trigrams(text: str) -> set[str]:
    t = f"  {_norm(text)} "
    return {t[i : i + 3] for i in range(len(t) - 2)}


def similarity(a: str, b: str) -> float:
    ta, tb = _trigrams(a), _trigrams(b)
    return len(ta & tb) / len(ta | tb) if ta and tb else 0.0


def duplicate_of(article: dict[str, Any], topic: str, history: list[dict[str, Any]]) -> str | None:
    """The past subject this article repeats, if any (continuations are allowed)."""
    if article.get("continuity_of"):
        return None
    urls = {s["url"] for s in article.get("sources", [])}
    ents = {_norm(e) for e in article.get("entities", []) if e.strip()}
    for h in history:
        if urls & set(h.get("urls", [])) and topic != "actu":
            return h.get("subject") or h["title"]
        if h.get("topic") != topic or topic == "actu":
            continue
        if similarity(article.get("subject", ""), h.get("subject", "")) >= DUPLICATE_SUBJECT:
            return h["subject"]
        past = {_norm(e) for e in h.get("entities", [])}
        shared = ents & past
        if len(shared) >= 2 and len(shared) / max(1, len(ents | past)) >= DUPLICATE_ENTITIES:
            return h.get("subject") or h["title"]
    return None


def check(
    article: dict[str, Any], topic: str, target_words: int, history: list[dict[str, Any]]
) -> tuple[dict[str, Any], list[str]]:
    if article.get("skip"):
        return article, []
    problems: list[str] = []
    body = article.get("body_markdown", "")
    if not article.get("title") or len(body.split()) < 30:
        problems.append("titre ou corps manquant.")
    words = len(body.split())
    if words > target_words * 1.6:
        problems.append(f"trop long ({words} mots pour ~{target_words} demandés) : resserre.")

    body_links = LINK.findall(body)
    sources = [s for s in article.get("sources", []) if s.get("url")]
    deep = [d for d in article.get("deep_dive", []) or [] if d.get("url")]
    all_urls = sorted({*body_links, *(s["url"] for s in sources), *(d["url"] for d in deep)})
    with ThreadPoolExecutor(8) as pool:
        status = dict(zip(all_urls, pool.map(web.check_url, all_urls)))

    dead_body = [u for u in body_links if not status[u][0]]
    if dead_body:
        problems.append(
            "liens morts ou inventés dans le texte : "
            + ", ".join(f"{u} ({status[u][1]})" for u in dead_body)
            + ". Remplace-les par des URL réellement consultées."
        )
    homepages = [u for u in body_links if web.is_homepage(u)]
    if homepages:
        problems.append(f"liens vers des pages d'accueil au lieu d'articles précis : {', '.join(homepages)}.")
    sources = [s for s in sources if status[s["url"]][0] and not web.is_homepage(s["url"])]
    if not sources and not body_links:
        problems.append("aucune source valide : l'article doit s'appuyer sur au moins une page web réelle.")
    if not body_links:
        problems.append("aucun lien dans le texte : cite tes sources en liens markdown.")
    article["sources"] = sources
    article["deep_dive"] = [d for d in deep if status[d["url"]][0]]
    article["unverified"] = sorted(u for u, (ok, why) in status.items() if ok and why != "ok")

    dup = duplicate_of(article, topic, history)
    if dup:
        problems.append(f"sujet trop proche d'un article déjà publié (« {dup} ») : choisis autre chose.")
    return article, problems
