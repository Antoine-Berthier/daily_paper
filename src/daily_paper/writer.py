"""Article writing: one headless `claude -p` call per article.

The model gets a self-contained brief (tone, topic instructions, the seed drawn
by the planner, what was already covered) and must answer through a JSON
schema. It only has WebSearch and WebFetch, and a lean system prompt instead
of Claude Code's default one (about 5× cheaper per call).
"""

from __future__ import annotations

import json
import subprocess
from datetime import date, timedelta
from typing import Any

from . import web
from .planner import ArticlePlan

SYSTEM_PROMPT = """\
Tu es rédacteur d'une newsletter quotidienne personnelle, en français.
Tu travailles comme un journaliste rigoureux : tu cherches (WebSearch), tu
ouvres et lis les sources (WebFetch), puis tu écris.

Règles absolues :
- Tout fait avancé doit venir d'une page web que tu as réellement ouverte ou
  vue dans les résultats de recherche. Cite-la en lien markdown dans le texte.
- N'invente jamais d'URL. Chaque lien doit pointer vers une page précise
  (article, billet, fiche), jamais vers une page d'accueil.
- Écris pour un lecteur curieux, en paragraphes courts. Markdown autorisé :
  **gras**, *italique*, listes, liens, et les blocs `:::tip`, `:::note`,
  `:::recette`, `:::attention` (fermés par `:::`). Pas de titre de niveau 1 ou 2.
- Respecte la longueur demandée (±20 %).
- Réponds uniquement via la sortie structurée demandée.
"""

ARTICLE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["skip", "title", "chapo", "body_markdown", "sources", "subject", "summary", "entities"],
    "properties": {
        "skip": {"type": "boolean", "description": "true seulement si la rubrique est optionnelle et que rien ne mérite d'être dit"},
        "skip_reason": {"type": "string"},
        "candidates": {
            "type": "array",
            "description": "échantillonnage verbalisé : idées candidates triées par probabilité décroissante",
            "items": {
                "type": "object",
                "required": ["idea", "probability"],
                "properties": {"idea": {"type": "string"}, "probability": {"type": "number"}},
            },
        },
        "title": {"type": "string", "description": "titre accrocheur, 4 à 12 mots"},
        "chapo": {"type": "string", "description": "une phrase d'accroche"},
        "body_markdown": {"type": "string"},
        "sources": {
            "type": "array",
            "description": "pages réellement consultées sur lesquelles s'appuie l'article",
            "items": {
                "type": "object",
                "required": ["title", "url"],
                "properties": {"title": {"type": "string"}, "url": {"type": "string"}},
            },
        },
        "deep_dive": {
            "type": "array",
            "description": "1 à 3 liens pour creuser le sujet, DIFFÉRENTS des sources citées : format long, vidéo, podcast, livre, tuto, carte…",
            "items": {
                "type": "object",
                "required": ["title", "url"],
                "properties": {"title": {"type": "string"}, "url": {"type": "string"}, "why": {"type": "string"}},
            },
        },
        "image": {
            "type": "object",
            "description": "une image pertinente : URL directe d'image vue dans une source, ou page dont l'image de partage illustre bien le sujet",
            "properties": {
                "image_url": {"type": "string"},
                "page_url": {"type": "string"},
                "alt": {"type": "string"},
                "credit": {"type": "string"},
            },
        },
        "subject": {"type": "string", "description": "sujet canonique court (ex. 'Le gqom de Durban'), sert à éviter les répétitions"},
        "summary": {"type": "string", "description": "résumé en une phrase pour l'historique"},
        "entities": {"type": "array", "items": {"type": "string"}, "description": "lieux, personnes, œuvres, organisations clés"},
        "continuity_of": {"type": "string", "description": "sujet d'un article précédent dont celui-ci est une vraie suite, sinon vide"},
    },
}


JOURS = ["lundi", "mardi", "mercredi", "jeudi", "vendredi", "samedi", "dimanche"]
MOIS = ["janvier", "février", "mars", "avril", "mai", "juin", "juillet", "août",
        "septembre", "octobre", "novembre", "décembre"]


def date_fr(d: date) -> str:
    return f"{JOURS[d.weekday()]} {d.day} {MOIS[d.month - 1]} {d.year}"


def _season(today: date) -> str:
    return ["hiver", "hiver", "printemps", "printemps", "printemps", "été", "été", "été",
            "automne", "automne", "automne", "hiver"][today.month - 1]


def _seed_text(seed: dict[str, Any]) -> str:
    lines = []
    for name, value in (seed.get("facets") or {}).items():
        lines.append(f"- {name} : {value}")
    if src := seed.get("source"):
        lines.append(
            f"- billet de départ tiré au hasard : « {src['title']} » ({src['source']}, {src.get('published') or 'date ?'})"
            f"\n  {src['url']}\n  extrait : {src.get('summary') or '—'}"
            "\n  Pars de ce billet si le sujet colle à la rubrique ; sinon sers-t'en comme tremplin"
            " vers un sujet voisin, ou ignore-le."
        )
    if angle := seed.get("angle"):
        lines.append(f"- angle d'écriture imposé : {angle}")
    if rank := seed.get("verbalized_rank"):
        n = seed["verbalized_of"]
        lines.append(
            f"- Méthode anti-banalité : avant d'écrire, liste {n} idées concrètes possibles pour cette"
            " rubrique et ce tirage, chacune avec la probabilité qu'un rédacteur typique la choisisse,"
            f" triées de la plus probable à la moins probable (champ candidates). Traite ensuite l'idée"
            f" n°{rank} de cette liste (pas une autre), en la vérifiant par la recherche. Si elle"
            " s'avère infondée ou déjà traitée, prends la suivante."
        )
    return "\n".join(lines) or "- aucun tirage : choisis librement, en évitant l'évident."


def _history_text(topic: str, history: list[dict[str, Any]], today: date) -> str:
    cutoff = (today - timedelta(days=180)).isoformat()
    rows = [h for h in history if h.get("topic") == topic and h.get("date", "") >= cutoff][-40:]
    if not rows:
        return "Rien encore."
    return "\n".join(f"- {h['date']} — {h.get('subject') or h['title']} : {h.get('summary', '')}" for h in rows)


def _headlines_text(spec: dict[str, Any]) -> str:
    cfg = spec.get("headlines")
    if not cfg:
        return ""
    rows = web.headlines(cfg["feeds"], int(cfg.get("max_age_hours", 36)))
    if not rows:
        return "\n## Titres récents\n(flux indisponibles : cherche toi-même via WebSearch)\n"
    lines = [f"- [{r['source']}] {r['title']} — {r['url']}" for r in rows]
    return "\n## Titres récents de flux RSS (pour juger l'importance par recoupement)\n" + "\n".join(lines) + "\n"


def build_prompt(
    art: ArticlePlan,
    settings: dict[str, Any],
    history: list[dict[str, Any]],
    siblings: list[ArticlePlan],
    today: date,
    feedback_note: str | None = None,
) -> str:
    spec = art.spec
    words = spec.get("words") or settings["words"]
    others = [f"- {s.spec.get('label', s.topic)} ({s.seed.get('facets') or ''})" for s in siblings if s.id != art.id]
    optional = (
        "Cette rubrique est optionnelle : si rien ne le justifie vraiment, mets skip à true."
        if spec.get("optional") else "Cette rubrique n'est pas optionnelle : skip doit rester false."
    )
    prompt = f"""\
Date : {date_fr(today)} ({_season(today)}). Lecteur : vit à Bruxelles.

# Rubrique : {spec.get('label', art.topic)}{f' — {art.subtype}' if art.subtype else ''}

## Consignes de la rubrique
{spec.get('instructions', '').strip()}

## Tirage du jour (imposé, c'est lui qui garantit la variété)
{_seed_text(art.seed)}

## Ton
{settings['tone'].strip()}

## Longueur
Environ {words} mots pour body_markdown. {optional}
{_headlines_text(spec)}
## Déjà traité dans cette rubrique (ne pas refaire, sauf vraie suite → continuity_of)
{_history_text(art.topic, history, today)}

## Autres rubriques de l'édition du jour (ne pas empiéter)
{chr(10).join(others) or '- aucune'}
"""
    if feedback_note:
        prompt += f"\n## Correction demandée (ta tentative précédente a été rejetée)\n{feedback_note}\n"
    return prompt


class WriterError(RuntimeError):
    pass


def call_claude(prompt: str, settings: dict[str, Any]) -> tuple[dict[str, Any], float]:
    """Run one headless Claude call; return (structured output, cost in USD)."""
    w = settings["writer"]
    argv = [
        "claude", "-p", prompt,
        "--model", str(w["model"]),
        "--system-prompt", SYSTEM_PROMPT,
        "--tools", "WebSearch,WebFetch",
        "--allowedTools", "WebSearch,WebFetch",
        "--strict-mcp-config", "--setting-sources", "",
        "--no-session-persistence", "--disable-slash-commands",
        "--max-budget-usd", str(w["budget_usd"]),
        "--output-format", "json",
        "--json-schema", json.dumps(ARTICLE_SCHEMA),
    ]
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=int(w["timeout_s"]))
    except subprocess.TimeoutExpired as exc:
        raise WriterError("timeout") from exc
    try:
        result = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise WriterError(f"sortie illisible (exit {proc.returncode}): {proc.stderr[-400:] or proc.stdout[-400:]}") from exc
    cost = float(result.get("total_cost_usd") or 0)
    out = result.get("structured_output")
    if result.get("is_error") or not isinstance(out, dict):
        raise WriterError(f"{result.get('subtype')}: {str(result.get('result'))[:400]}")
    return out, cost
