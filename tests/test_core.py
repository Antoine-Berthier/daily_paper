import random
from collections import Counter
from datetime import date, datetime

import pytest

from daily_paper import config, customize, pipeline, planner, quota, render, state, validate, web


def fresh():
    import json

    return json.loads(json.dumps(state.EMPTY))


# ---------------------------------------------------------------- planner


def test_bag_draws_every_value_before_repeating():
    st, rng = fresh(), random.Random(0)
    values = list("abcdef")
    first = [planner.draw_bag(st, "k", values, rng) for _ in values]
    assert sorted(first) == values
    second = [planner.draw_bag(st, "k", values, rng) for _ in values]
    assert sorted(second) == values


def test_recency_avoids_recent_values():
    rng, today = random.Random(1), date(2026, 9, 28)
    picks = Counter()
    for _ in range(200):
        st = fresh()
        st["last_used"]["k"] = {"a": "2026-09-27"}
        picks[planner.draw_recency(st, "k", ["a", "b"], rng, today)] += 1
    assert picks["b"] > 190


def test_credit_scheduling_matches_probabilities():
    topics = config.topics()
    settings = config.settings()
    st, rng = fresh(), random.Random(42)
    n = 600
    counts = Counter()
    for _ in range(n):
        counts.update(planner.choose_topics(topics, settings, st, rng))
    for name, spec in topics.items():
        if spec.get("always"):
            assert counts[name] == n
        else:
            assert abs(counts[name] / n - spec["probability"]) < 0.06, name


def test_edition_size_respects_bounds():
    topics, settings = config.topics(), config.settings()
    st, rng = fresh(), random.Random(3)
    always = sum(1 for t in topics.values() if t.get("always"))
    for _ in range(200):
        chosen = planner.choose_topics(topics, settings, st, rng)
        magazine = len(chosen) - always
        assert settings["articles"]["min"] <= magazine <= settings["articles"]["max"]


def test_bandit_factor_range():
    st, rng = fresh(), random.Random(0)
    st["bandit"]["x"] = [50, 1]
    assert all(0.6 <= planner.bandit_factor(st, "x", rng) <= 1.4 for _ in range(100))
    assert sum(planner.bandit_factor(st, "x", rng) for _ in range(100)) / 100 > 1.3


def test_merged_spec_layers_subtype():
    spec = config.merged_spec(config.topics()["cuisine"], "recette")
    assert "cuisine" in spec["seed"]["facets"] and spec["words"] == 230


# ---------------------------------------------------------------- validate


def test_similarity_detects_near_duplicates():
    assert validate.similarity("Le gqom de Durban", "Le gqom, son de Durban") > 0.55
    assert validate.similarity("Le gqom de Durban", "La cumbia villera") < 0.3


def test_duplicate_of_respects_continuity():
    past = [{"topic": "musique", "subject": "Le gqom de Durban", "title": "t", "entities": [], "urls": []}]
    art = {"subject": "Le gqom de Durban", "entities": [], "sources": []}
    assert validate.duplicate_of(art, "musique", past)
    assert validate.duplicate_of({**art, "continuity_of": "Le gqom"}, "musique", past) is None


# ---------------------------------------------------------------- customize


def test_customize_roundtrip_keeps_comments():
    before = config.TOPICS_FILE.read_text(encoding="utf-8")
    customize.set_probability("cuisine", 0.5)
    after = config.TOPICS_FILE.read_text(encoding="utf-8")
    assert "# Pool de rubriques." in after
    assert config.topics()["cuisine"]["probability"] == 0.5
    customize.set_probability("cuisine", 0.33)
    assert config.TOPICS_FILE.read_text(encoding="utf-8").count("\n") == before.count("\n")


def test_customize_add_remove_topic_and_pool():
    customize.add_topic("bd", "Bulles", 0.2, "Présente une bande dessinée indépendante récente, sourcée.")
    assert "bd" in config.topics()
    customize.remove_topic("bd")
    assert "bd" not in config.topics()
    customize.add_pool_values("crafts", ["kumihimo"])
    assert "kumihimo" in config.pool("crafts")
    customize.remove_pool_values("crafts", ["kumihimo"])
    assert "kumihimo" not in config.pool("crafts")


def test_customize_rejects_bad_values():
    with pytest.raises(customize.ToolError):
        customize.set_probability("cuisine", 3)
    with pytest.raises(customize.ToolError):
        customize.set_probability("inconnue", 0.5)
    with pytest.raises(customize.ToolError):
        customize.set_article_count(4, 2)


# ---------------------------------------------------------------- gate / render


def test_gate_blocks_on_recent_unread_and_weekends():
    settings = config.settings()
    st = fresh()
    monday = datetime(2026, 9, 28, 8, 0)
    assert pipeline.gate(st, settings, monday, auto=True) is None
    assert pipeline.gate(st, settings, datetime(2026, 9, 26, 8), auto=True) == "pas un jour de parution"
    st["editions"]["2026-09-25"] = {"date": "2026-09-25", "read_at": None}
    assert "pas encore lue" in pipeline.gate(st, settings, monday, auto=True)
    st["editions"]["2026-09-25"]["read_at"] = "x"
    assert pipeline.gate(st, settings, monday, auto=True) is None
    st["editions"]["2026-09-10"] = {"date": "2026-09-10", "read_at": None}  # too old to block
    assert pipeline.gate(st, settings, monday, auto=True) is None


def test_render_containers_and_links():
    html = render.render_edition({
        "id": "2026-09-28", "name": "Daily Paper", "date_label": "Lundi 28 septembre 2026", "number": 1,
        "articles": [{"id": "a1", "label": "En cuisine", "title": "T", "chapo": "C",
                      "body_markdown": "Texte [src](https://ex.com/a).\n\n:::tip\nAstuce\n:::\n",
                      "sources": [{"title": "S", "url": "https://ex.com/a"}], "deep_dive": []}],
    })
    assert 'class="box box-tip"' in html and 'target="_blank"' in html


# ---------------------------------------------------------------- dico2rue


def _word(word, definition="une définition correcte", up=20, down=2, pid="1"):
    return {"word": word, "definition": definition, "example": "", "votedfor": str(up),
            "votesagainst": str(down), "pageId": pid, "pageSlug": "x"}


def test_pick_expression_skips_published_entries():
    words = [_word("Mc crado", "Mc Donald"), _word("Déjà vu")]
    rng = random.Random(0)
    assert {web.pick_expression(words, {"Déjà vu"}, rng)["expression"] for _ in range(50)} == {"Mc crado"}
    assert web.pick_expression(words, {"Déjà vu", "Mc crado"}, rng) is None


def test_verbatim_topic_needs_no_writer():
    art = planner.ArticlePlan(id="a", topic="expression", subtype=None, spec={"writer": "none"},
                              seed={"expression": {"expression": "Mc crado", "definition": "Mc Donald",
                                                   "example": "", "url": "https://www.dico2rue.com/x/"}})
    out, cost = pipeline._write_one(art, None, config.settings(), [], date(2026, 9, 28))
    assert cost == 0 and out["title"] == "Mc crado" and out["kind"] == "verbatim"
    html = render.render_edition({"id": "2026-09-28", "name": "D", "date_label": "L", "number": 1,
                                  "articles": [{**out, "id": "a", "label": "L'expression du jour"}]})
    assert "Mc Donald" in html and "dico2rue" in html


# ---------------------------------------------------------------- quota


def test_quota_usage_delta_and_reset():
    before = {"five_hour": {"utilization": 0.26, "resetsAt": 1}, "seven_day": {"utilization": 0.69, "resetsAt": 9}}
    after = {"five_hour": {"utilization": 0.04, "resetsAt": 2}, "seven_day": {"utilization": 0.71, "resetsAt": 9}}
    used = quota.usage(before, after)
    assert used["seven_day"] == {"before": 69, "after": 71, "points": 2, "reset": False}
    assert used["five_hour"]["reset"] and used["five_hour"]["points"] == 4
    assert quota.usage(None, after) is None


def test_footer_shows_cost_and_quota():
    html = render.render_edition({"id": "2026-09-28", "name": "D", "date_label": "L", "number": 1, "articles": [],
                                  "cost_usd": 0.654, "quota": {"five_hour": {"before": 26, "after": 29, "points": 3, "reset": False},
                                                               "seven_day": {"before": 69, "after": 69, "points": 0, "reset": False}}})
    assert "0,65 $" in html and "session 5 h : 3 % du quota (26 → 29 %)" in html and "semaine : &lt; 1 % du quota" in html


# ---------------------------------------------------------------- puzzle

from daily_paper import puzzle  # noqa: E402

PUZZLE = {
    "function_name": "survivor",
    "starter_code": "def survivor(n, k):\n    pass\n",
    "reference_solution": "def survivor(n, k):\n    people = list(range(1, n + 1))\n    i = 0\n"
                          "    while len(people) > 1:\n        i = (i + k - 1) % len(people)\n        people.pop(i)\n"
                          "    return people[0]\n",
    "examples": [{"args": "(7, 3)", "expected": "4"}],
    "tests": [{"args": "(1, 5)", "expected": "1"}, {"args": "(10, 2)", "expected": "5"}, {"args": "(5, 1)", "expected": "5"},
              {"args": "(41, 3)", "expected": "31"}],
}


def test_reference_puzzle_is_accepted():
    assert puzzle.check_generated(dict(PUZZLE)) == []


def test_wrong_expected_value_is_caught():
    bad = {**PUZZLE, "tests": PUZZLE["tests"][:3] + [{"args": "(41, 3)", "expected": "30"}]}
    assert "échoue" in puzzle.check_generated(bad)[0]


def test_example_leaking_a_hidden_answer_is_rejected():
    bad = {**PUZZLE, "tests": PUZZLE["tests"][:3] + [{"args": "(7,3)", "expected": "4"}]}
    assert "args différents" in puzzle.check_generated(bad)[0]


def test_starter_that_already_solves_is_rejected():
    bad = {**PUZZLE, "starter_code": PUZZLE["reference_solution"] + "# def survivor(\n"}
    assert "passe déjà" in puzzle.check_generated(bad)[0]


def test_run_reports_errors_output_and_infinite_loops():
    res = puzzle.run("def survivor(n, k):\n    print('dbg')\n    return 1 // 0\n", "survivor", PUZZLE["examples"])
    assert not res["results"][0]["ok"] and "ZeroDivisionError" in res["results"][0]["error"] and "ligne 3" in res["results"][0]["error"]
    assert "dbg" in res["stdout"]
    res = puzzle.run("def survivor(n, k):\n    while True: pass\n", "survivor", PUZZLE["examples"])
    assert "error" in res
    assert "introuvable" in puzzle.run("x = 1", "survivor", PUZZLE["examples"])["error"]


def test_call_repr():
    assert render.call_repr("(3, [1, 2])", "f") == "f(3, [1, 2])"
    assert render.call_repr("(5,)", "f") == "f(5)" and render.call_repr("()", "f") == "f()"


def test_pools_hold_only_strings():
    # A stray "key : value" in a pool silently turns an entry into a dict.
    for name in config.pool_names():
        values = config.pool(name)
        items = [x for v in values.values() for x in v] if isinstance(values, dict) else values
        assert all(isinstance(x, str) for x in items), name
