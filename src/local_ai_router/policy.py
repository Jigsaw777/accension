"""Named presets compile to ordinary typed policy; one routing implementation."""
from __future__ import annotations

from .config import Routing, ControlPlane

PRESETS = {
    "maximum-savings": {"quality_slos": {"simple": .8, "coding": .85, "architecture": .9, "critical": .98, "explanation": .8}, "local_first": True, "local_execution_bonus": .05},
    "balanced": {"quality_slos": {"simple": .85, "coding": .9, "architecture": .94, "critical": .98, "explanation": .85}, "local_first": True, "local_execution_bonus": .02},
    "quality-first": {"quality_slos": {"simple": .92, "coding": .96, "architecture": .98, "critical": .99, "explanation": .92}, "local_first": False, "local_execution_bonus": 0},
    "fully-local": {"local_first": True},
    "local-control": {"local_first": True},
    "custom": {},
}


def preset(settings, name):
    if name not in PRESETS:
        raise ValueError("Unknown routing preset")
    routing = Routing.model_validate({**settings.routing.model_dump(), **PRESETS[name], "preset": name})
    control = ControlPlane.model_validate({**settings.control_plane.model_dump(), "fully_local": name == "fully-local",
        "routing_location": "local-only" if name in {"fully-local", "local-control"} else settings.control_plane.routing_location})
    return routing, control
