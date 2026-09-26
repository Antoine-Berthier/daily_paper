"""`daily-paper` command line."""

from __future__ import annotations

import argparse
import json
import logging
import random
import shutil
import sys
from collections import Counter
from pathlib import Path

from . import config


def _setup_logging(verbose: bool) -> None:
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s", "%Y-%m-%d %H:%M:%S")
    logger = logging.getLogger("daily_paper")
    logger.setLevel(logging.INFO)
    file_handler = logging.FileHandler(config.DATA_DIR / "daily_paper.log", encoding="utf-8")
    file_handler.setFormatter(fmt)
    console = logging.StreamHandler(sys.stderr)
    console.setFormatter(fmt)
    console.setLevel(logging.INFO if verbose else logging.WARNING)
    logger.addHandler(file_handler)
    logger.addHandler(console)


def _topics_arg(value: str | None) -> list[str] | None:
    return [t.strip() for t in value.split(",") if t.strip()] if value else None


def cmd_run(args: argparse.Namespace) -> int:
    from . import notify, pipeline

    eid = pipeline.run(force=args.force or bool(args.topics), only=_topics_arg(args.topics),
                       auto=False, send_notification=not args.no_notify)
    if not eid:
        print("Pas d'édition produite (détails : data/daily_paper.log).")
        return 1
    print(f"Édition {eid} : {config.EDITIONS_DIR / eid / 'index.html'}")
    print(f"Lien : {notify.edition_url(eid, config.settings())}")
    return 0


def cmd_plan(args: argparse.Namespace) -> int:
    """Dry run: show today's plan, or simulate N editions to check frequencies."""
    from . import planner, state

    st, history = state.load(), state.history()
    if args.simulate:
        # Frequencies only: no seeds, no network, nothing persisted.
        topics, settings, rng = config.topics(), config.settings(), random.Random(args.seed)
        count: Counter[str] = Counter()
        sizes: Counter[int] = Counter()
        for _ in range(args.simulate):
            chosen = planner.choose_topics(topics, settings, st, rng)
            count.update(chosen)
            sizes[len(chosen)] += 1
        print(f"Sur {args.simulate} éditions simulées :")
        for name, spec in topics.items():
            target = "toujours" if spec.get("always") else f"visé {float(spec.get('probability') or 0):.0%}"
            print(f"  {name:<12} {count[name] / args.simulate:>5.0%}  ({target})")
        print("  taille des éditions : " + ", ".join(f"{k} articles ×{v}" for k, v in sorted(sizes.items())))
        return 0
    plan = planner.plan(st, history, only=_topics_arg(args.topics), rng=random.Random(args.seed))
    for a in plan.articles:
        print(f"● {a.spec.get('label', a.topic)}{' / ' + a.subtype if a.subtype else ''}")
        print("  " + json.dumps(a.seed, ensure_ascii=False, indent=2).replace("\n", "\n  "))
    return 0


def cmd_serve(args: argparse.Namespace) -> int:
    from . import server

    server.serve()
    return 0


def cmd_feedback(args: argparse.Namespace) -> int:
    from . import feedback

    rows = feedback.pending()
    if not args.apply:
        print(f"{len(rows)} retour(s) en attente.")
        for r in rows:
            print("  " + json.dumps(r, ensure_ascii=False))
        return 0
    changes = feedback.process_pending()
    print("\n".join(f"- {c}" for c in changes) or "Rien à appliquer.")
    return 0


def cmd_cfg(args: argparse.Namespace) -> int:
    from . import customize, feedback

    if args.tool in (None, "list"):
        for t in customize.TOOLS:
            params = ", ".join(f"{k}{'' if k in t.required else '?'}" for k in t.parameters)
            print(f"{t.name}({params})\n    {t.description}")
        return 0
    try:
        payload = json.loads(args.json) if args.json else {}
        print(customize.call(args.tool, payload))
    except (customize.ToolError, json.JSONDecodeError, TypeError) as exc:
        print(f"Erreur : {exc}", file=sys.stderr)
        return 2
    if args.commit and feedback.commit_config(f"cfg: {args.tool} {args.json or ''}"[:72]):
        print("(commité)")
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    from . import feedback, state

    st = state.load()
    for eid, meta in sorted(st["editions"].items())[-10:]:
        print(f"{eid}  {'lue ' if meta.get('read_at') else 'NON LUE'}  {', '.join(meta.get('topics', []))}  "
              f"{meta.get('cost_usd', 0):.2f} $")
    print(f"crédits : { {k: round(v, 2) for k, v in st['credits'].items()} }")
    print(f"bandit : {st['bandit']}")
    print(f"retours en attente : {len(feedback.pending())}")
    return 0


def cmd_notify_test(args: argparse.Namespace) -> int:
    from . import notify

    ok = notify.send("Daily Paper — test", ["Si tu vois ceci, les notifications marchent."],
                     f"http://localhost:{config.settings()['server']['port']}/")
    print("notification envoyée" if ok else "échec de la notification")
    return 0 if ok else 1


def cmd_rerender(args: argparse.Namespace) -> int:
    from . import pipeline

    print(f"{pipeline.rerender_all()} édition(s) re-rendue(s)")
    return 0


def cmd_install_windows(args: argparse.Namespace) -> int:
    """Start the server at Windows logon (hidden), via the user's Startup folder."""
    import subprocess

    appdata = subprocess.run(["cmd.exe", "/c", "echo %APPDATA%"], capture_output=True, text=True,
                             cwd="/mnt/c").stdout.strip()
    if not appdata or "%" in appdata:
        print("Impossible de trouver %APPDATA% (pas sous WSL ?)", file=sys.stderr)
        return 1
    win_startup = appdata + r"\Microsoft\Windows\Start Menu\Programs\Startup"
    startup = Path(subprocess.run(["wslpath", "-u", win_startup], capture_output=True, text=True).stdout.strip())
    target = startup / "daily_paper.vbs"
    if args.uninstall:
        target.unlink(missing_ok=True)
        print(f"supprimé : {target}")
        return 0
    import os

    distro = os.environ.get("WSL_DISTRO_NAME", "Ubuntu")
    uv = shutil.which("uv") or str(Path.home() / ".local/bin/uv")
    command = f"cd {config.ROOT} && exec {uv} run --all-extras daily-paper serve >> data/server.out 2>&1"
    vbs = (
        "' Daily Paper : démarre le serveur local + planificateur dans WSL, fenêtre cachée.\r\n"
        'Set sh = CreateObject("WScript.Shell")\r\n'
        f'sh.Run "wsl.exe -d {distro} --exec bash -lc ""{command}""", 0, False\r\n'
    )
    target.write_text(vbs, encoding="utf-8")
    print(f"installé : {target}")
    if args.start:
        subprocess.Popen(["wscript.exe", subprocess.run(["wslpath", "-w", str(target)], capture_output=True,
                                                          text=True).stdout.strip()])
        print("serveur lancé")
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="daily-paper", description="Newsletter quotidienne générée par agents.")
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="command", required=True)

    r = sub.add_parser("run", help="produire une édition (respecte les règles sauf --force)")
    r.add_argument("--force", action="store_true", help="ignorer jour de parution et éditions non lues")
    r.add_argument("--topics", help="rubriques imposées, séparées par des virgules (implique --force)")
    r.add_argument("--no-notify", action="store_true")
    r.set_defaults(fn=cmd_run)

    pl = sub.add_parser("plan", help="aperçu du tirage sans rien rédiger ni enregistrer")
    pl.add_argument("--topics")
    pl.add_argument("--simulate", type=int, metavar="N", help="simuler N éditions (fréquences des rubriques)")
    pl.add_argument("--seed", type=int)
    pl.set_defaults(fn=cmd_plan)

    sub.add_parser("serve", help="serveur local + parution automatique").set_defaults(fn=cmd_serve)

    fb = sub.add_parser("feedback", help="voir / appliquer les retours en attente")
    fb.add_argument("--apply", action="store_true")
    fb.set_defaults(fn=cmd_feedback)

    c = sub.add_parser("cfg", help="outils de personnalisation (ceux de l'agent de feedback)")
    c.add_argument("tool", nargs="?", help="nom de l'outil, ou 'list'")
    c.add_argument("json", nargs="?", help="arguments JSON, ex. '{\"topic\": \"cuisine\", \"probability\": 0.5}'")
    c.add_argument("--commit", action="store_true", help="commiter la modification dans git")
    c.set_defaults(fn=cmd_cfg)

    sub.add_parser("status", help="éditions récentes, crédits, bandit").set_defaults(fn=cmd_status)
    sub.add_parser("notify-test", help="envoyer une notification de test").set_defaults(fn=cmd_notify_test)
    sub.add_parser("rerender", help="régénérer le HTML des éditions existantes").set_defaults(fn=cmd_rerender)

    w = sub.add_parser("install-windows", help="lancer le serveur à l'ouverture de session Windows")
    w.add_argument("--uninstall", action="store_true")
    w.add_argument("--start", action="store_true", help="le démarrer tout de suite")
    w.set_defaults(fn=cmd_install_windows)

    args = p.parse_args(argv)
    _setup_logging(args.verbose or args.command in ("run", "serve"))
    return args.fn(args)
