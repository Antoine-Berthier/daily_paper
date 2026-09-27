"""Logic puzzles: run a `solve`-style function against test cases.

Used twice: at generation, to prove the puzzle is sound (the reference solution
passes every test, the starter code does not), and from the edition page, when
the reader clicks "Exécuter".

Code runs in a separate `python -I` process with CPU, memory and wall-clock
limits, in an empty temp directory. That keeps runaway loops and memory bombs
contained, but it is not a security sandbox: it is only meant for the reader's
own code, which is why the server accepts runs from its own pages only.
"""

from __future__ import annotations

import ast
import json
import resource
import subprocess
import sys
import tempfile
from typing import Any

TIMEOUT_S = 10
MEMORY_BYTES = 1 << 30

HARNESS = r'''
import ast, contextlib, copy, io, json, math, sys, traceback

spec = json.loads(sys.stdin.read())
out = io.StringIO()

def where(exc):
    frames = [f for f in traceback.extract_tb(exc.__traceback__) if f.filename == "solution.py"]
    return f" (ligne {frames[-1].lineno})" if frames else ""

def same(a, b):
    if isinstance(a, float) or isinstance(b, float):
        try:
            return math.isclose(a, b, rel_tol=1e-6, abs_tol=1e-9)
        except TypeError:
            return False
    if isinstance(a, (list, tuple)) and isinstance(b, (list, tuple)):
        return len(a) == len(b) and all(same(x, y) for x, y in zip(a, b))
    if isinstance(a, dict) and isinstance(b, dict):
        return a.keys() == b.keys() and all(same(a[k], b[k]) for k in a)
    return a == b

ns = {"__name__": "solution"}
try:
    with contextlib.redirect_stdout(out):
        exec(compile(spec["code"], "solution.py", "exec"), ns)
except Exception as exc:
    print(json.dumps({"error": f"{type(exc).__name__}: {exc}{where(exc)}", "stdout": out.getvalue()[-2000:]}))
    sys.exit(0)
fn = ns.get(spec["function"])
if not callable(fn):
    print(json.dumps({"error": f"la fonction {spec['function']}() est introuvable", "stdout": out.getvalue()[-2000:]}))
    sys.exit(0)
results = []
for case in spec["tests"]:
    args = ast.literal_eval(case["args"])
    args = args if isinstance(args, tuple) else (args,)
    expected = ast.literal_eval(case["expected"])
    try:
        with contextlib.redirect_stdout(out):
            got = fn(*copy.deepcopy(args))
        results.append({"ok": same(got, expected), "got": repr(got)[:300]})
    except Exception as exc:
        results.append({"ok": False, "error": f"{type(exc).__name__}: {exc}{where(exc)}"})
print(json.dumps({"results": results, "stdout": out.getvalue()[-2000:]}))
'''


def _limits() -> None:
    resource.setrlimit(resource.RLIMIT_CPU, (TIMEOUT_S, TIMEOUT_S))
    resource.setrlimit(resource.RLIMIT_AS, (MEMORY_BYTES, MEMORY_BYTES))


def run(code: str, function: str, tests: list[dict[str, str]]) -> dict[str, Any]:
    """{"results": [{"ok", "got"|"error"}…], "stdout"} or {"error", "stdout"}."""
    spec = json.dumps({"code": code, "function": function, "tests": tests})
    with tempfile.TemporaryDirectory(prefix="dp_puzzle_") as tmp:
        try:
            proc = subprocess.run(
                [sys.executable, "-I", "-c", HARNESS], input=spec, capture_output=True, text=True,
                timeout=TIMEOUT_S + 2, cwd=tmp, env={"PATH": "/usr/bin:/bin"}, preexec_fn=_limits,
            )
        except subprocess.TimeoutExpired:
            return {"error": f"trop long (> {TIMEOUT_S} s) : boucle infinie ?", "stdout": ""}
    try:
        return json.loads(proc.stdout.strip().splitlines()[-1])
    except (json.JSONDecodeError, IndexError):
        reason = "mémoire ou temps de calcul dépassé" if proc.returncode < 0 else proc.stderr.strip()[-300:]
        return {"error": reason or "exécution interrompue", "stdout": ""}


def cases(article: dict[str, Any]) -> list[dict[str, Any]]:
    """Visible examples first, then hidden tests, each flagged."""
    return [{**c, "visible": True} for c in article.get("examples", [])] + [
        {**c, "visible": False} for c in article.get("tests", [])
    ]


def check_generated(article: dict[str, Any]) -> list[str]:
    """Problems that make a generated puzzle unusable (fed back to the writer)."""
    fn = article.get("function_name", "")
    if not fn.isidentifier():
        return [f"function_name invalide : {fn!r}."]
    if f"def {fn}(" not in article.get("starter_code", ""):
        return [f"starter_code doit définir {fn}(…)."]
    all_cases = cases(article)
    if not article.get("examples") or len(article.get("tests", [])) < 4:
        return ["il faut au moins un exemple visible et 4 tests cachés."]
    signatures = [ "".join(c.get("args", "").split()) for c in all_cases]
    if len(set(signatures)) < len(signatures) or "()" in signatures:
        return ["chaque cas doit avoir des args différents (et au moins un argument) : "
                "sinon un exemple visible donne la réponse d'un test caché."]
    problems = []
    for c in all_cases:
        for key in ("args", "expected"):
            try:
                ast.literal_eval(c[key])
            except (ValueError, SyntaxError, KeyError):
                problems.append(f"{key} n'est pas un littéral Python valide : {c.get(key)!r}.")
    if problems:
        return problems
    ref = run(article.get("reference_solution", ""), fn, all_cases)
    if "error" in ref:
        return [f"la solution de référence ne s'exécute pas : {ref['error']}."]
    failed = [(c, r) for c, r in zip(all_cases, ref["results"]) if not r["ok"]]
    if failed:
        return ["la solution de référence échoue sur certains cas (corrige les attendus ou la solution) : "
                + "; ".join(f"{c['args']} → obtenu {r.get('got', r.get('error'))}, attendu {c['expected']}"
                            for c, r in failed[:5])]
    starter = run(article["starter_code"], fn, all_cases)
    if "results" in starter and all(r["ok"] for r in starter["results"]):
        return ["le code de départ passe déjà tous les tests : il ne doit pas contenir la solution."]
    return []
