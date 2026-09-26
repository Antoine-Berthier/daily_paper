"""Edition rendering: article JSON → one clean, self-contained HTML page.

Bodies are "augmented Markdown": CommonMark plus `:::tip|note|recette|attention`
container blocks. External links open in a new tab and are reported to the
local server (implicit feedback).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader, select_autoescape
from markdown_it import MarkdownIt
from mdit_py_plugins.container import container_plugin

CONTAINERS = {"tip": "Astuce", "note": "À noter", "recette": "La recette", "attention": "Attention"}
TEMPLATES = Path(__file__).parent / "templates"


def _container_renderer(name: str, label: str):
    def render(self, tokens, idx, options, env):
        if tokens[idx].nesting == 1:
            return f'<aside class="box box-{name}"><div class="box-label">{label}</div>\n'
        return "</aside>\n"

    return render


def _markdown() -> MarkdownIt:
    md = MarkdownIt("commonmark", {"linkify": False, "typographer": True}).enable(["table", "strikethrough"])
    for name, label in CONTAINERS.items():
        md.use(container_plugin, name, render=_container_renderer(name, label))
    default_open = md.renderer.rules.get("link_open")

    def link_open(self, tokens, idx, options, env):
        tokens[idx].attrSet("target", "_blank")
        tokens[idx].attrSet("rel", "noopener")
        if default_open:
            return default_open(self, tokens, idx, options, env)
        return self.renderToken(tokens, idx, options, env)

    md.add_render_rule("link_open", link_open)
    return md


MD = _markdown()
ENV = Environment(loader=FileSystemLoader(TEMPLATES), autoescape=select_autoescape(["html", "j2"]))
ENV.filters["md"] = lambda text: MD.render(text or "")


def render_edition(edition: dict[str, Any]) -> str:
    return ENV.get_template("edition.html.j2").render(e=edition)


def render_index(editions: list[dict[str, Any]], topics: dict[str, Any], status: dict[str, Any]) -> str:
    return ENV.get_template("index.html.j2").render(editions=editions, topics=topics, status=status)
