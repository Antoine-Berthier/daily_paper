"""Deterministic web access: RSS feeds, URL checks, og:image, dico2rue.

Everything here runs in Python, before or after the LLM, so that seeds and
checks do not depend on the model's goodwill.
"""

from __future__ import annotations

import html
import json
import random
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import urljoin, urlparse

import feedparser
import httpx

UA = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/140.0 Safari/537.36 daily-paper/0.1",
    "Accept-Language": "fr-BE,fr;q=0.9,en;q=0.8",
}


def get(url: str, timeout: float = 15.0) -> httpx.Response:
    return httpx.get(url, headers=UA, follow_redirects=True, timeout=timeout)


def strip_html(text: str, limit: int = 500) -> str:
    text = html.unescape(re.sub(r"<[^>]+>", " ", text or ""))
    return " ".join(text.split())[:limit]


# ---------------------------------------------------------------- feeds


def _entry_date(entry: Any) -> datetime | None:
    for key in ("published_parsed", "updated_parsed"):
        parsed = entry.get(key)
        if parsed:
            return datetime(*parsed[:6], tzinfo=timezone.utc)
    return None


def feed_entries(url: str, max_age: timedelta | None = None, limit: int = 40) -> list[dict[str, Any]]:
    """Recent entries of one feed; [] on any failure."""
    try:
        parsed = feedparser.parse(get(url).content)
    except Exception:
        return []
    source = parsed.feed.get("title") or urlparse(url).netloc
    cutoff = datetime.now(timezone.utc) - max_age if max_age else None
    rows = []
    for entry in parsed.entries[:limit]:
        when = _entry_date(entry)
        if cutoff and when and when < cutoff:
            continue
        link = entry.get("link")
        if not link or not entry.get("title"):
            continue
        rows.append({
            "title": strip_html(entry.title, 200),
            "url": link,
            "published": when.isoformat() if when else None,
            "summary": strip_html(entry.get("summary", ""), 400),
            "source": strip_html(source, 60),
        })
    return rows


def headlines(feeds: list[str], max_age_hours: int, per_feed: int = 25) -> list[dict[str, Any]]:
    """Recent headlines of several feeds, fetched in parallel."""
    age = timedelta(hours=max_age_hours)
    with ThreadPoolExecutor(8) as pool:
        batches = pool.map(lambda u: feed_entries(u, age)[:per_feed], feeds)
    return [row for batch in batches for row in batch]


def random_feed_item(
    feeds: list[str], max_age_days: int, exclude_urls: set[str], rng: random.Random
) -> dict[str, Any] | None:
    """A random recent, never-used entry from a random feed (tries up to 4 feeds)."""
    order = list(feeds)
    rng.shuffle(order)
    for url in order[:4]:
        items = [e for e in feed_entries(url, timedelta(days=max_age_days)) if e["url"] not in exclude_urls]
        if items:
            return rng.choice(items)
    return None


# ---------------------------------------------------------------- checks


def check_url(url: str) -> tuple[bool, str]:
    """(usable, reason). Bot-blocking statuses count as usable but unverified."""
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        return False, "URL invalide"
    try:
        r = get(url, timeout=20)
    except Exception as exc:
        return False, f"injoignable ({type(exc).__name__})"
    if r.status_code < 400:
        return True, "ok"
    if r.status_code in (401, 402, 403, 405, 429, 451, 503):
        return True, f"non vérifiable (HTTP {r.status_code})"
    return False, f"HTTP {r.status_code}"


def is_homepage(url: str) -> bool:
    return urlparse(url).path.strip("/") in ("", "fr", "en", "info", "news", "actualites")


# ---------------------------------------------------------------- images

_META_IMAGE = re.compile(
    r"<meta[^>]+(?:property|name)=[\"'](?:og:image(?::url)?|twitter:image(?::src)?)[\"'][^>]*>", re.I
)
_CONTENT = re.compile(r"content=[\"']([^\"']+)[\"']", re.I)
_EXT = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp", "image/gif": ".gif", "image/avif": ".avif"}


def page_image(page_url: str) -> str | None:
    """The og:image / twitter:image of a page, absolute."""
    try:
        r = get(page_url)
    except Exception:
        return None
    if r.headers.get("content-type", "").startswith("image/"):
        return str(r.url)
    for tag in _META_IMAGE.findall(r.text[:300_000]):
        m = _CONTENT.search(tag)
        if m:
            return urljoin(str(r.url), html.unescape(m.group(1)))
    return None


def download_image(url: str, max_bytes: int = 6_000_000) -> tuple[bytes, str] | None:
    """(bytes, extension) if the URL serves a reasonably sized image."""
    try:
        r = get(url, timeout=25)
    except Exception:
        return None
    ctype = r.headers.get("content-type", "").split(";")[0].strip()
    if r.status_code >= 400 or ctype not in _EXT or not (2_000 < len(r.content) <= max_bytes):
        return None
    return r.content, _EXT[ctype]


# ---------------------------------------------------------------- dico2rue

DICO2RUE = "https://www.dico2rue.com"
_NEXT_DATA = re.compile(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', re.S)
# Letters weighted roughly by how many entries they hold.
_LETTERS = "AABBBCCCDDEEFFGGHIJKLMMMNOPPPQRSSSTTTUVWYZ"


def _dico2rue_words(path: str) -> list[dict[str, Any]]:
    try:
        m = _NEXT_DATA.search(get(DICO2RUE + path).text)
        return json.loads(m.group(1))["props"]["pageProps"].get("words") or [] if m else []
    except Exception:
        return []


def dico2rue_candidates(exclude: set[str], rng: random.Random, n: int = 6) -> list[dict[str, Any]]:
    """Up to n unused expressions from random dico2rue listing pages.

    Votes are a weak signal of funniness, so the writer gets several candidates
    and picks the funniest non-insulting one; randomness still comes from here.
    """
    found: dict[str, dict[str, Any]] = {}
    for _ in range(8):
        if len(found) >= n:
            break
        letter = rng.choice(_LETTERS)
        page = rng.randint(1, 12)
        words = _dico2rue_words(f"/dictionnaire/alphabet/{letter}/page-{page}/") or _dico2rue_words(
            f"/dictionnaire/alphabet/{letter}/"
        )
        good = [w for w in words if w.get("word") not in exclude and int(w.get("votedfor") or 0) >= 5]
        # Multi-word expressions make better stories than single slang words.
        good.sort(key=lambda w: (" " not in w["word"].strip(), rng.random()))
        for w in good[:2]:
            found[w["word"]] = {
                "expression": w["word"],
                "definition": strip_html(w.get("definition", ""), 400),
                "example": strip_html(w.get("example", ""), 300),
                "url": f"{DICO2RUE}/dictionnaire/mot/{w['pageId']}/{w['pageSlug']}/",
            }
    items = list(found.values())
    rng.shuffle(items)
    return items[:n]
