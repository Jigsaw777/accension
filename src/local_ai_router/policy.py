"""Named presets compile to ordinary typed policy; one routing implementation."""

from __future__ import annotations

from .config import ControlPlane, Routing

PRESETS = {
    "maximum-savings": {
        "quality_slos": {"simple": 0.8, "coding": 0.85, "architecture": 0.9, "critical": 0.98, "explanation": 0.8},
        "local_first": True,
        "local_execution_bonus": 0.05,
    },
    "balanced": {
        "quality_slos": {"simple": 0.85, "coding": 0.9, "architecture": 0.94, "critical": 0.98, "explanation": 0.85},
        "local_first": True,
        "local_execution_bonus": 0.02,
    },
    "quality-first": {
        "quality_slos": {"simple": 0.92, "coding": 0.96, "architecture": 0.98, "critical": 0.99, "explanation": 0.92},
        "local_first": False,
        "local_execution_bonus": 0,
    },
    "fully-local": {"local_first": True},
    "local-control": {"local_first": True},
    "custom": {},
}


def preset(settings, name):
    if name not in PRESETS:
        raise ValueError("Unknown routing preset")
    routing = Routing.model_validate({**settings.routing.model_dump(), **PRESETS[name], "preset": name})
    control = ControlPlane.model_validate(
        {
            **settings.control_plane.model_dump(),
            "fully_local": name == "fully-local",
            "routing_location": "local-only"
            if name in {"fully-local", "local-control"}
            else settings.control_plane.routing_location,
        }
    )
    return routing, control
