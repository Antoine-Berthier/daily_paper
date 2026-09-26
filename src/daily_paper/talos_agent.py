"""Run the feedback agent on TALOS with only the customization tools.

TALOS is used as a library: its `Agent` loop gets a registry holding nothing
but `customize.TOOLS` (no file, shell or web access), in autonomous mode.
"""

from __future__ import annotations

from typing import Any

from . import config, customize


def _registry():
    from talos.tools.base import Tool, ToolRegistry

    registry = ToolRegistry()
    for spec in customize.TOOLS:
        def run(self, ctx, _spec=spec, **kwargs: Any) -> str:
            try:
                return _spec.fn(**{k: v for k, v in kwargs.items() if v is not None})
            except customize.ToolError as exc:
                return f"Erreur : {exc}"
            except TypeError as exc:
                return f"Erreur d'arguments : {exc}"

        cls = type(
            f"Tool_{spec.name}", (Tool,),
            {"name": spec.name, "description": spec.description, "parameters": spec.parameters,
             "required": spec.required, "run": run},
        )
        registry.register(cls())
    return registry


def run_tools_agent(prompt: str, settings: dict[str, Any]) -> str:
    from talos.agent import Agent
    from talos.autonomy import AutonomyMode
    from talos.config import Config, load_dotenv

    load_dotenv()
    cfg = Config.resolve(preset=settings["feedback"].get("talos_preset"))
    agent = Agent(
        config=cfg, cwd=config.ROOT, registry=_registry(), autonomy=AutonomyMode.AUTONOMOUS,
        use_memory=False, max_steps=30, max_minutes=10, compact_at=None,
    )
    return agent.run(prompt).final_text
