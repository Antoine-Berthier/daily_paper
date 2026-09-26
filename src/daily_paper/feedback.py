"""Feedback loop.

1. Ratings (👍/👎) and link clicks → a Thompson bandit per topic and subtype
   (deterministic, no LLM). The planner turns it into a weight factor in
   [0.6, 1.4], so feedback shifts frequencies without ever silencing a topic.
2. Free-text comments → a light agent (TALOS, cheap model) that can only call
   the tools of `customize.py`. Changes are committed to git.
3. Anything the tools cannot express → `escalate` → headless Claude on a
   branch; merged on main only if the test suite passes.
"""

from __future__ import annotations

import logging
import re
import subprocess
from datetime import datetime
from typing import Any

from . import config, customize, state

log = logging.getLogger("daily_paper")

AGENT_PROMPT = """\
Tu es l'agent de réglage de « Daily Paper », une newsletter quotidienne personnelle
en français générée par des agents. Le lecteur a laissé des retours ; applique-les
avec tes outils, et seulement avec eux.

Méthode :
1. Appelle show_config (et show_topic pour les rubriques concernées).
2. Pour chaque retour, fais la modification minimale qui y répond. Interprète
   raisonnablement : « plus court » ≈ -30 % de mots, « plus souvent » ≈ ×1.5 la
   probabilité (max 1), « moins » ≈ ×0.6. Un retour sur UN article concerne sa
   rubrique, pas toute la newsletter.
3. Si un retour exige autre chose que ces outils (modifier le code, la mise en
   page, une nouvelle source de tirage spéciale, un nouvel outil…), appelle
   escalate avec une demande précise et autonome.
4. Ignore les retours purement positifs ou sans consigne actionnable.
5. Termine par un résumé d'une ligne par changement effectué, en français,
   commençant par « - ».

Retours du lecteur :
{items}
"""


def _article_index() -> dict[str, dict[str, Any]]:
    return {h["article"]: h for h in state.history() if h.get("article")}


def _update_bandit(st: dict[str, Any], keys: list[str], alpha: float = 0, beta: float = 0) -> None:
    for key in keys:
        a, b = st["bandit"].get(key, [1.0, 1.0])
        st["bandit"][key] = [round(a + alpha, 3), round(b + beta, 3)]


def _git(*args: str, check: bool = False) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=config.ROOT, capture_output=True, text=True, check=check)


def commit_config(message: str) -> bool:
    """Commit config/ if it changed; True when a commit was made."""
    if not _git("status", "--porcelain", "config").stdout.strip():
        return False
    _git("add", "config")
    _git("commit", "-m", message)
    return True


def recent_config_changes(n: int = 5) -> list[str]:
    out = _git("log", f"-{n}", "--format=%ad %s", "--date=format:%d/%m", "--", "config").stdout
    return [line for line in out.splitlines() if line.strip()]


def pending() -> list[dict[str, Any]]:
    return state.feedback()[state.load()["feedback_offset"]:]


def process_pending(*, use_agent: bool = True) -> list[str]:
    """Apply everything received since the last run; return a change log."""
    rows = pending()
    if not rows:
        return []
    index = _article_index()
    ratings: dict[str, int] = {}
    clicked: set[str] = set()
    comments: list[str] = []
    for row in rows:
        art = index.get(row.get("article") or "")
        where = f"[article « {art['title']} », rubrique {art['topic']}{'/' + art['subtype'] if art.get('subtype') else ''}]" if art else "[édition entière]"
        if "rating" in row and row.get("article"):
            ratings[row["article"]] = int(row["rating"])
        if row.get("click") and row.get("article"):
            clicked.add(row["article"])
        if row.get("text"):
            comments.append(f"- {where} {row['text']}")

    changes: list[str] = []
    with state.transaction() as st:
        for aid, rating in ratings.items():
            art = index.get(aid)
            if not art or rating == 0:
                continue
            keys = [art["topic"]] + ([f"{art['topic']}/{art['subtype']}"] if art.get("subtype") else [])
            _update_bandit(st, keys, alpha=1 if rating > 0 else 0, beta=1 if rating < 0 else 0)
            changes.append(f"note {'+' if rating > 0 else '-'} sur {'/'.join(keys[-1:])}")
        for aid in clicked:
            if art := index.get(aid):
                _update_bandit(st, [art["topic"]], alpha=0.25)
        st["feedback_offset"] += len(rows)

    if comments and use_agent:
        changes += apply_comments(comments)
    return changes


def apply_comments(comments: list[str]) -> list[str]:
    settings = config.settings()
    prompt = AGENT_PROMPT.format(items="\n".join(comments))
    customize.ESCALATIONS.clear()
    summary = ""
    if settings["feedback"].get("agent", "talos") == "talos":
        try:
            from .talos_agent import run_tools_agent

            summary = run_tools_agent(prompt, settings)
        except Exception as exc:
            log.warning("agent TALOS indisponible (%s) : escalade directe vers Claude", exc)
            customize.ESCALATIONS.append("Applique ces retours du lecteur :\n" + "\n".join(comments))
    else:
        customize.ESCALATIONS.append("Applique ces retours du lecteur :\n" + "\n".join(comments))

    lines = [l.strip("- ").strip() for l in summary.splitlines() if l.strip().startswith("-")]
    title = lines[0] if len(lines) == 1 else f"{len(lines)} réglages" if lines else "réglages"
    if commit_config(f"feedback: {title[:70]}\n\n" + "\n".join(f"- {l}" for l in lines) + "\n\nRetours :\n" + "\n".join(comments)):
        log.info("config modifiée par l'agent :\n%s", summary)
    changes = list(lines)
    for request in list(customize.ESCALATIONS):
        changes.append(escalate(request, settings))
    customize.ESCALATIONS.clear()
    return changes


# ---------------------------------------------------------------- escalation

ESCALATION_PROMPT = """\
Tu fais évoluer le projet « Daily Paper » (ce dépôt ; lis CLAUDE.md d'abord).
Demande issue des retours du lecteur :

{request}

Contraintes :
- Réglages simples → modifie config/*.yaml (ou `uv run daily-paper cfg <outil> '<json>'`).
- Changement de code → garde le style existant, ajoute/ajuste un test dans tests/.
- `uv run pytest -q` doit passer à la fin.
- Ne touche ni à data/ ni à editions/, ne fais ni commit ni push (c'est géré après toi).
Termine par un résumé en 1 à 3 lignes de ce que tu as changé.
"""


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower())[:40].strip("-") or "demande"


def escalate(request: str, settings: dict[str, Any]) -> str:
    """Run headless Claude on a branch; merge if tests pass. Returns a change-log line."""
    if _git("status", "--porcelain", "--", "src", "config", "tests").stdout.strip():
        log.warning("escalade annulée : modifications locales non commitées")
        return f"escalade en attente (arbre git non propre) : {request[:80]}"
    base = _git("rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
    branch = f"feedback/{datetime.now():%Y%m%d-%H%M}-{_slug(request)}"
    created = _git("checkout", "-b", branch)
    if created.returncode != 0:
        log.warning("escalade impossible : %s", created.stderr.strip())
        return f"escalade impossible (git) : {request[:80]}"
    try:
        proc = subprocess.run(
            ["claude", "-p", ESCALATION_PROMPT.format(request=request),
             "--model", str(settings["feedback"].get("escalation_model", "opus")),
             "--permission-mode", "acceptEdits",
             "--allowedTools", "Read,Edit,Write,Glob,Grep,Bash(uv run pytest:*),Bash(uv run daily-paper:*),Bash(git diff:*),Bash(git status:*)",
             "--no-session-persistence", "--output-format", "text"],
            cwd=config.ROOT, capture_output=True, text=True, timeout=1800,
        )
        summary = (proc.stdout or proc.stderr).strip().splitlines()[-3:]
        tests = subprocess.run(["uv", "run", "pytest", "-q"], cwd=config.ROOT, capture_output=True, text=True, timeout=600)
        _git("add", "-A", "src", "config", "tests", "CLAUDE.md", "README.md")
        committed = _git("commit", "-m", f"feedback(escalade): {request[:60]}\n\n{request}\n\n" + "\n".join(summary)).returncode == 0
    finally:
        _git("checkout", base)
    if not committed:
        _git("branch", "-D", branch)
        return f"escalade sans effet : {request[:80]}"
    if tests.returncode == 0 and settings["feedback"].get("auto_merge", True):
        if _git("merge", "--ff-only", branch).returncode == 0:
            _git("branch", "-d", branch)
            if settings["feedback"].get("auto_push"):
                _git("push")
            from . import notify
            notify.send("Daily Paper — évolution appliquée", [request[:120]], "http://localhost:%d/" % settings["server"]["port"])
            return f"escalade appliquée : {' '.join(summary)[:160]}"
    from . import notify
    notify.send("Daily Paper — évolution à relire", [f"branche {branch} (tests {'OK' if tests.returncode == 0 else 'en échec'})"],
                "http://localhost:%d/" % settings["server"]["port"])
    return f"escalade à relire sur la branche {branch}"
