"""Model DNA: stable observations with visible uncertainty and bounded specialization."""

from __future__ import annotations

import json
import time

from pydantic import Field

from .schema import Strict

DIMENSIONS = (
    "classification",
    "planning",
    "architecture",
    "coding",
    "debugging",
    "testing",
    "review",
    "repair",
    "tool_use",
    "structured_output",
    "instruction_following",
    "long_context",
    "documentation",
    "reasoning",
    "reliability",
    "cost_efficiency",
)
ALIASES = {
    "json_adherence": "structured_output",
    "reviewing": "review",
    "simple": "coding",
    "critical": "coding",
    "explanation": "documentation",
}


class Evidence(Strict):
    value: float | None = Field(default=None, ge=0, le=1)
    samples: int = 0
    uncertainty: str = "unknown"
    sources: dict[str, int] = {}
    updated_at: float | None = None


class ModelDNA(Strict):
    schema_version: int = 1
    model: str
    dimensions: dict[str, Evidence]
    specializations: dict[str, dict[str, Evidence]] = {}
    operational: dict = {}
    economics: dict = {}
    resources: dict = {}
    metadata_provenance: str


def observe(store, model, dimension, passed, source="verified_task", specialization=""):
    dimension = ALIASES.get(dimension, dimension)
    if dimension not in DIMENSIONS:
        return
    with store.transaction():
        row = store.db.execute(
            "SELECT * FROM dna WHERE model=? AND dimension=? AND specialization=?", (model, dimension, specialization)
        ).fetchone()
        n, value, sources = (row["samples"], row["value"], json.loads(row["sources"])) if row else (0, 0.5, {})
        sources[source] = sources.get(source, 0) + 1
        # Five percent EWMA starts neutral. A single result moves only .025.
        value = 0.95 * value + 0.05 * int(passed)
        store.db.execute(
            "INSERT OR REPLACE INTO dna VALUES(?,?,?,?,?,?,?)",
            (model, dimension, specialization, n + 1, value, json.dumps(sources), time.time()),
        )


def profile(store, model):
    dimensions = {d: Evidence() for d in DIMENSIONS}
    specializations = {}
    for row in store.db.execute("SELECT * FROM dna WHERE model=? ORDER BY dimension,specialization", (model.id,)):
        evidence = Evidence(
            value=row["value"],
            samples=row["samples"],
            sources=json.loads(row["sources"]),
            uncertainty="limited" if row["samples"] < 10 else "moderate" if row["samples"] < 30 else "observed",
            updated_at=row["stamp"],
        )
        if not row["specialization"]:
            dimensions[row["dimension"]] = evidence
        elif row["samples"] >= 5:
            specializations.setdefault(row["specialization"], {})[row["dimension"]] = evidence
    return ModelDNA(
        model=model.id,
        dimensions=dimensions,
        specializations=specializations,
        operational={
            "latency": model.expected_latency,
            "ttft": model.ttft,
            "throughput": model.tokens_per_second,
            "context_window": model.context_window,
            "reliability_metadata": model.reliability,
        },
        economics={
            "input_cost": model.input_price,
            "output_cost": model.output_price,
            "cache_cost": model.cached_input_price,
            "pricing_source": model.pricing_source,
            "unit": "USD per million tokens",
        },
        resources={"ram_mb": model.ram_estimate_mb, "vram_mb": model.vram_estimate_mb, "loaded": model.loaded},
        metadata_provenance=model.capabilities_source,
    ).model_dump()


def estimate(store, model, dimension, prior):
    dimension = ALIASES.get(dimension, dimension)
    row = store.db.execute(
        "SELECT samples,value FROM dna WHERE model=? AND dimension=? AND specialization=''", (model, dimension)
    ).fetchone()
    if not row or row["samples"] < 5:
        return prior
    weight = min(0.6, row["samples"] / 100)
    return prior * (1 - weight) + row["value"] * weight


def reset(store, model=None):
    with store.transaction():
        store.db.execute("DELETE FROM dna" + (" WHERE model=?" if model else ""), (model,) if model else ())
    return {"status": "reset", "model": model, "metadata_preserved": True}
