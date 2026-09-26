"""Customization tools: the only way the light feedback agent touches config.

Each tool is a plain function over the YAML files (round-tripped with
ruamel so comments and layout survive), described once in `TOOLS`. The same
table feeds the TALOS adapter, the `daily-paper cfg` CLI and the prompt of
the Claude escalation, so they cannot drift apart.
"""

from __future__ import annotations

import io
import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ruamel.yaml import YAML

from . import config

_yaml = YAML()
_yaml.preserve_quotes = True
_yaml.width = 4096
_yaml.indent(mapping=2, sequence=4, offset=2)

ESCALATIONS: list[str] = []  # filled by the `escalate` tool during an agent run


class ToolError(ValueError):
    pass


def _load(path: Path) -> Any:
    with path.open(encoding="utf-8") as f:
        return _yaml.load(f)


def _dump(path: Path, data: Any) -> None:
    buf = io.StringIO()
    _yaml.dump(data, buf)
    path.write_text(buf.getvalue(), encoding="utf-8")


def _topic(data: Any, topic: str, subtype: str | None = None) -> Any:
    topics = data["topics"]
    if topic not in topics:
        raise ToolError(f"rubrique inconnue '{topic}'. Rubriques : {', '.join(topics)}")
    node = topics[topic]
    if subtype:
        subs = node.get("subtypes") or {}
        if subtype not in subs:
            raise ToolError(f"sous-type inconnu '{subtype}' pour {topic}. Sous-types : {', '.join(subs) or 'aucun'}")
        node = subs[subtype]
    return node


def _range(value: Any, lo: float, hi: float, what: str) -> float:
    try:
        v = float(value)
    except (TypeError, ValueError) as exc:
        raise ToolError(f"{what} doit être un nombre") from exc
    if not lo <= v <= hi:
        raise ToolError(f"{what} doit être entre {lo} et {hi}")
    return v


# ---------------------------------------------------------------- tools


def show_config() -> str:
    """Readable summary of the current configuration."""
    s = config.settings()
    lines = [
        f"ton : {s['tone']}",
        f"articles magazine par édition : {s['articles']['min']} à {s['articles']['max']}",
        f"longueur par défaut : {s['words']} mots",
        f"horaire : {s['schedule']['time']}, jours {s['schedule']['weekdays']}",
        "rubriques :",
    ]
    for name, t in config.topics().items():
        freq = "toujours" if t.get("always") else f"p={t.get('probability', 0)}"
        lines.append(f"- {name} « {t.get('label')} » ({freq}{', optionnelle' if t.get('optional') else ''}, "
                     f"{t.get('words', s['words'])} mots)")
        for sub, sv in (t.get("subtypes") or {}).items():
            lines.append(f"    · sous-type {sub} (poids {sv.get('weight', 1)}, {sv.get('words', '-')} mots)")
    lines.append(f"pools de tirage : {', '.join(config.pool_names())}")
    return "\n".join(lines)


def show_topic(topic: str) -> str:
    """Full YAML of one topic (instructions, seeds, feeds, subtypes)."""
    data = _load(config.TOPICS_FILE)
    node = _topic(data, topic)
    buf = io.StringIO()
    _yaml.dump({topic: node}, buf)
    return buf.getvalue()


def show_pool(pool: str) -> str:
    if pool not in config.pool_names():
        raise ToolError(f"pool inconnu '{pool}'. Pools : {', '.join(config.pool_names())}")
    return (config.POOLS_DIR / f"{pool}.yaml").read_text(encoding="utf-8")


def set_article_count(min: int, max: int) -> str:  # noqa: A002 — tool parameter names
    lo, hi = int(_range(min, 0, 8, "min")), int(_range(max, 1, 8, "max"))
    if lo > hi:
        raise ToolError("min doit être ≤ max")
    data = _load(config.SETTINGS_FILE)
    data["articles"]["min"], data["articles"]["max"] = lo, hi
    _dump(config.SETTINGS_FILE, data)
    return f"articles magazine : {lo} à {hi}"


def set_words(words: int, topic: str | None = None, subtype: str | None = None) -> str:
    w = int(_range(words, 40, 900, "words"))
    if not topic:
        data = _load(config.SETTINGS_FILE)
        data["words"] = w
        _dump(config.SETTINGS_FILE, data)
        return f"longueur par défaut : {w} mots"
    data = _load(config.TOPICS_FILE)
    _topic(data, topic, subtype)["words"] = w
    _dump(config.TOPICS_FILE, data)
    return f"longueur de {topic}{'/' + subtype if subtype else ''} : {w} mots"


def set_tone(tone: str) -> str:
    if len(tone.strip()) < 10:
        raise ToolError("décris le ton en une ou plusieurs phrases")
    data = _load(config.SETTINGS_FILE)
    data["tone"] = tone.strip()
    _dump(config.SETTINGS_FILE, data)
    return "ton mis à jour"


def set_probability(topic: str, probability: float) -> str:
    p = _range(probability, 0, 1, "probability")
    data = _load(config.TOPICS_FILE)
    node = _topic(data, topic)
    if node.get("always"):
        node["always"] = False
    node["probability"] = round(p, 3)
    _dump(config.TOPICS_FILE, data)
    return f"{topic} : probabilité {p:.2f} (≈ une édition sur {1 / p:.1f})" if p else f"{topic} désactivée (p=0)"


def set_always(topic: str, always: bool) -> str:
    data = _load(config.TOPICS_FILE)
    _topic(data, topic)["always"] = bool(always)
    _dump(config.TOPICS_FILE, data)
    return f"{topic} : {'toujours présente' if always else 'tirée selon sa probabilité'}"


def set_subtype_weight(topic: str, subtype: str, weight: float) -> str:
    w = _range(weight, 0, 10, "weight")
    data = _load(config.TOPICS_FILE)
    _topic(data, topic, subtype)["weight"] = w
    _dump(config.TOPICS_FILE, data)
    return f"{topic}/{subtype} : poids {w}"


def set_instructions(topic: str, instructions: str, subtype: str | None = None) -> str:
    if len(instructions.strip()) < 20:
        raise ToolError("instructions trop courtes")
    data = _load(config.TOPICS_FILE)
    from ruamel.yaml.scalarstring import LiteralScalarString

    _topic(data, topic, subtype)["instructions"] = LiteralScalarString(instructions.strip() + "\n")
    _dump(config.TOPICS_FILE, data)
    return f"instructions de {topic}{'/' + subtype if subtype else ''} remplacées"


def add_topic(id: str, label: str, probability: float, instructions: str, words: int | None = None) -> str:  # noqa: A002
    if not re.fullmatch(r"[a-z][a-z0-9_]{1,30}", id):
        raise ToolError("id : minuscules, chiffres et _ (ex. 'bd', 'science_fun')")
    data = _load(config.TOPICS_FILE)
    if id in data["topics"]:
        raise ToolError(f"la rubrique {id} existe déjà")
    from ruamel.yaml.comments import CommentedMap
    from ruamel.yaml.scalarstring import LiteralScalarString

    node = CommentedMap()
    node["label"] = label
    node["probability"] = round(_range(probability, 0, 1, "probability"), 3)
    if words:
        node["words"] = int(_range(words, 40, 900, "words"))
    node["seed"] = CommentedMap(angle=True)
    node["instructions"] = LiteralScalarString(instructions.strip() + "\n")
    data["topics"][id] = node
    _dump(config.TOPICS_FILE, data)
    return f"rubrique {id} « {label} » ajoutée (p={node['probability']})"


def remove_topic(topic: str) -> str:
    data = _load(config.TOPICS_FILE)
    _topic(data, topic)
    del data["topics"][topic]
    _dump(config.TOPICS_FILE, data)
    return f"rubrique {topic} supprimée (récupérable dans l'historique git)"


def add_facet(topic: str, name: str, values: list[str], subtype: str | None = None, strategy: str = "bag") -> str:
    """Add or replace an inline facet (a list of values drawn at random for the seed)."""
    if strategy not in ("bag", "recency", "uniform"):
        raise ToolError("strategy : bag | recency | uniform")
    if len(values) < 3:
        raise ToolError("au moins 3 valeurs, sinon il n'y a pas de diversité")
    from ruamel.yaml.comments import CommentedMap

    data = _load(config.TOPICS_FILE)
    node = _topic(data, topic, subtype)
    seed = node.setdefault("seed", CommentedMap())
    facets = seed.setdefault("facets", CommentedMap())
    facets[name] = CommentedMap(values=list(values), strategy=strategy)
    _dump(config.TOPICS_FILE, data)
    return f"facette {name} ({len(values)} valeurs) sur {topic}{'/' + subtype if subtype else ''}"


def _pool_edit(pool: str, values: list[str], add: bool) -> str:
    path = config.POOLS_DIR / f"{pool}.yaml"
    if not path.exists():
        if not add:
            raise ToolError(f"pool inconnu '{pool}'")
        path.write_text("[]\n", encoding="utf-8")
    items = _load(path) or []
    if not isinstance(items, list):
        raise ToolError(f"le pool {pool} n'est pas une simple liste")
    if add:
        new = [v for v in values if v not in items]
        items.extend(new)
        msg = f"{len(new)} valeur(s) ajoutée(s) à {pool} ({len(items)} au total)"
    else:
        before = len(items)
        for v in values:
            while v in items:
                items.remove(v)
        msg = f"{before - len(items)} valeur(s) retirée(s) de {pool}"
    _dump(path, items)
    return msg


def add_pool_values(pool: str, values: list[str]) -> str:
    return _pool_edit(pool, values, add=True)


def remove_pool_values(pool: str, values: list[str]) -> str:
    return _pool_edit(pool, values, add=False)


def add_feed(topic: str, url: str, kind: str = "seed", subtype: str | None = None) -> str:
    """kind=seed: posts drawn as starting points; kind=headlines: titles given for context."""
    if not re.match(r"https?://", url):
        raise ToolError("URL http(s) attendue")
    from ruamel.yaml.comments import CommentedMap

    data = _load(config.TOPICS_FILE)
    node = _topic(data, topic, subtype)
    if kind == "headlines":
        feeds = node.setdefault("headlines", CommentedMap(max_age_hours=48, feeds=[])).setdefault("feeds", [])
    else:
        feeds = node.setdefault("seed", CommentedMap()).setdefault("feeds", [])
    if url not in feeds:
        feeds.append(url)
    _dump(config.TOPICS_FILE, data)
    return f"flux ajouté à {topic} ({kind})"


def remove_feed(topic: str, url: str) -> str:
    data = _load(config.TOPICS_FILE)
    node = _topic(data, topic)
    removed = 0
    for holder in [node.get("headlines"), node.get("seed"), *[(s or {}).get("seed") for s in (node.get("subtypes") or {}).values()]]:
        if holder and url in (holder.get("feeds") or []):
            holder["feeds"].remove(url)
            removed += 1
    if not removed:
        raise ToolError("flux introuvable dans cette rubrique")
    _dump(config.TOPICS_FILE, data)
    return f"flux retiré de {topic}"


def set_schedule(time: str | None = None, weekdays: list[int] | None = None) -> str:
    data = _load(config.SETTINGS_FILE)
    if time:
        if not re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", time):
            raise ToolError("heure au format HH:MM")
        data["schedule"]["time"] = time
    if weekdays is not None:
        if not weekdays or any(d not in range(7) for d in weekdays):
            raise ToolError("jours : entiers 0 (lundi) à 6 (dimanche)")
        data["schedule"]["weekdays"] = sorted(set(weekdays))
    _dump(config.SETTINGS_FILE, data)
    return f"horaire : {data['schedule']['time']}, jours {list(data['schedule']['weekdays'])}"


def escalate(request: str) -> str:
    """Hand a change the tools cannot express to Claude (code changes, new tools…)."""
    if len(request.strip()) < 15:
        raise ToolError("décris précisément la modification attendue")
    ESCALATIONS.append(request.strip())
    return "demande transmise à Claude (elle sera traitée après toi)"


# ---------------------------------------------------------------- registry


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    parameters: dict[str, Any]
    required: list[str]
    fn: Callable[..., str]


def _s(desc: str) -> dict[str, Any]:
    return {"type": "string", "description": desc}


def _n(desc: str) -> dict[str, Any]:
    return {"type": "number", "description": desc}


_LIST = {"type": "array", "items": {"type": "string"}}

TOOLS: list[ToolSpec] = [
    ToolSpec("show_config", "Résumé de la configuration actuelle (ton, nombre d'articles, rubriques, probabilités).", {}, [], show_config),
    ToolSpec("show_topic", "YAML complet d'une rubrique (instructions, tirages, flux, sous-types).", {"topic": _s("id de rubrique")}, ["topic"], show_topic),
    ToolSpec("show_pool", "Contenu d'un pool de tirage (liste de valeurs).", {"pool": _s("nom du pool")}, ["pool"], show_pool),
    ToolSpec("set_article_count", "Nombre d'articles magazine par édition (hors rubriques 'toujours').", {"min": _n("minimum"), "max": _n("maximum")}, ["min", "max"], set_article_count),
    ToolSpec("set_words", "Longueur cible en mots : par défaut (sans topic) ou pour une rubrique/sous-type.", {"words": _n("mots"), "topic": _s("rubrique (optionnel)"), "subtype": _s("sous-type (optionnel)")}, ["words"], set_words),
    ToolSpec("set_tone", "Remplace la description du ton d'écriture.", {"tone": _s("description complète du ton")}, ["tone"], set_tone),
    ToolSpec("set_probability", "Fréquence d'une rubrique entre 0 et 1 (0.33 ≈ une édition sur trois ; 0 = désactivée).", {"topic": _s("rubrique"), "probability": _n("0..1")}, ["topic", "probability"], set_probability),
    ToolSpec("set_always", "Rend une rubrique toujours présente, ou la remet au tirage.", {"topic": _s("rubrique"), "always": {"type": "boolean"}}, ["topic", "always"], set_always),
    ToolSpec("set_subtype_weight", "Poids relatif d'un sous-type dans sa rubrique (ex. cuisine/recette vs astuce).", {"topic": _s("rubrique"), "subtype": _s("sous-type"), "weight": _n("poids ≥ 0")}, ["topic", "subtype", "weight"], set_subtype_weight),
    ToolSpec("set_instructions", "Remplace les instructions de recherche/écriture d'une rubrique ou d'un sous-type. Lis-les d'abord avec show_topic et conserve ce qui reste valable.", {"topic": _s("rubrique"), "instructions": _s("texte complet"), "subtype": _s("sous-type (optionnel)")}, ["topic", "instructions"], set_instructions),
    ToolSpec("add_topic", "Crée une nouvelle rubrique.", {"id": _s("identifiant court, ex. 'bd'"), "label": _s("titre affiché"), "probability": _n("0..1"), "instructions": _s("consignes de recherche et d'écriture, sourcées"), "words": _n("longueur (optionnel)")}, ["id", "label", "probability", "instructions"], add_topic),
    ToolSpec("remove_topic", "Supprime une rubrique.", {"topic": _s("rubrique")}, ["topic"], remove_topic),
    ToolSpec("add_facet", "Ajoute une facette de tirage aléatoire (liste de valeurs) à une rubrique, pour diversifier ses sujets.", {"topic": _s("rubrique"), "name": _s("nom de la facette"), "values": {**_LIST, "description": "au moins 3 valeurs"}, "subtype": _s("sous-type (optionnel)"), "strategy": _s("bag (défaut) | recency | uniform")}, ["topic", "name", "values"], add_facet),
    ToolSpec("add_pool_values", "Ajoute des valeurs à un pool de tirage (régions, genres musicaux, artisanats…).", {"pool": _s("nom du pool"), "values": _LIST}, ["pool", "values"], add_pool_values),
    ToolSpec("remove_pool_values", "Retire des valeurs d'un pool de tirage.", {"pool": _s("nom du pool"), "values": _LIST}, ["pool", "values"], remove_pool_values),
    ToolSpec("add_feed", "Ajoute un flux RSS à une rubrique (kind=seed : billets tirés comme point de départ ; kind=headlines : titres fournis pour l'actualité).", {"topic": _s("rubrique"), "url": _s("URL du flux"), "kind": _s("seed | headlines"), "subtype": _s("sous-type (optionnel)")}, ["topic", "url"], add_feed),
    ToolSpec("remove_feed", "Retire un flux RSS d'une rubrique.", {"topic": _s("rubrique"), "url": _s("URL du flux")}, ["topic", "url"], remove_feed),
    ToolSpec("set_schedule", "Heure (HH:MM) et/ou jours (0=lundi … 6=dimanche) de parution automatique.", {"time": _s("HH:MM"), "weekdays": {"type": "array", "items": {"type": "integer"}}}, [], set_schedule),
    ToolSpec("escalate", "Transmet à Claude une demande que les outils ne couvrent pas (changer le code, la mise en page, ajouter un outil ou une source spéciale…).", {"request": _s("description précise et autonome de la modification")}, ["request"], escalate),
]

BY_NAME = {t.name: t for t in TOOLS}


def call(name: str, args: dict[str, Any]) -> str:
    tool = BY_NAME.get(name)
    if tool is None:
        raise ToolError(f"outil inconnu '{name}'")
    return tool.fn(**{k: v for k, v in args.items() if v is not None})
