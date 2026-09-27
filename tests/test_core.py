import random
from collections import Counter
from datetime import date, datetime

import pytest

from daily_paper import config, customize, pipeline, planner, render, state, validate, web


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
