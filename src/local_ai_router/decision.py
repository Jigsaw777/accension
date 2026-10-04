"""Local, reproducible task analysis. No inference, networking or model dependency."""

from __future__ import annotations

import re
from pathlib import PurePosixPath

from .schema import Classification


class LocalDecisionEngine:
    def decide(self, task: str, graph=None, history=None) -> Classification:
        text = task.lower()
        critical = bool(
            re.search(r"security|credential|authenticat|migration|concurren|race condition|deadlock|encrypt", text)
        )
        architecture = bool(re.search(r"architect|multi.module|across|redesign|api.*database", text))
        feature = bool(re.search(r"\bfeature\b|\bimplement\b", text))
        simple = bool(re.search(r"typo|rename|format|documentation|explain|unit test", text))
        family = (
            "critical"
            if critical
            else "architecture"
            if architecture
            else "explanation"
            if "explain" in text
            else "simple"
            if simple
            else "coding"
        )
        complexity = 80 if architecture or critical else 10 if simple else 35
        risk = 85 if critical else 45 if architecture else 10 if simple else 25
        files = getattr(graph, "files", {})
        mentioned = [name for name in files if name.lower() in text]
        affected = set(mentioned)
        edges = getattr(graph, "edges", [])
        for edge in edges:
            if edge["source"] in mentioned or edge["target"] in mentioned:
                affected.update((edge["source"], edge["target"]))
        languages = {PurePosixPath(name).suffix for name in affected}
        modules = {str(PurePosixPath(name).parent) for name in affected}
        estimated_files = len(affected) or (8 if architecture else 1)
        complexity = min(100, complexity + max(0, estimated_files - 2) * 2 + max(0, len(languages) - 1) * 4)
        schema_change = bool(re.search(r"schema|database|public api|breaking change", text))
        if schema_change:
            risk = min(100, risk + 10)
        failures = int((history or {}).get("failures", 0))
        if failures:
            risk = min(100, risk + min(15, failures * 3))
        planning = architecture or critical or feature or estimated_files > 3 or complexity >= 65
        sources = ["static_analysis"]
        if graph is not None:
            sources.append("repository_graph")
        if history:
            sources.append("historical_profile")
        capabilities = ["text"] if family == "explanation" else ["code"]
        if re.search(r"\b(image|screenshot|vision)\b", text):
            capabilities.append("vision")
        if re.search(r"\b(json|schema output|structured output)\b", text):
            capabilities.append("structured_output")
        return Classification(
            task_family=family,
            complexity=complexity,
            risk=risk,
            planning_required=planning,
            review_required=risk >= 50,
            estimated_files=min(100, estimated_files),
            requires_frontier_planner=complexity >= 65,  # V1 compatibility hint only.
            recommended_tier=4 if architecture or critical else 1,
            confidence=0.9 if simple or critical else 0.7,
            planning_depth="FULL_ARCHITECTURE_PLAN"
            if complexity >= 75
            else "IMPLEMENTATION_PLAN"
            if planning
            else "NONE"
            if simple
            else "MICRO_PLAN",
            required_capabilities=capabilities,
            decision_source=sources,
            reason_codes=["DETERMINISTIC_PRIOR", "LOCAL_POLICY"],
            signals={
                "request_characters": len(task),
                "affected_files": len(affected),
                "graph_fan_out": max(0, len(affected) - len(mentioned)),
                "languages": len(languages),
                "modules": len(modules),
                "schema_or_public_api": schema_change,
                "tests_available": bool(graph and graph.repo.validation),
                "historical_failures": failures,
                "generated_files": any("generated" in p or p.endswith(".lock") for p in affected),
            },
        )


class ArbitrationPolicy:
    def __init__(self, routing):
        self.routing = routing

    def needed(self, decision, failures=0):
        p = self.routing
        return (
            decision.confidence < p.classifier_confidence_threshold
            or decision.risk >= p.arbiter_risk_threshold
            or decision.task_family in {"architecture", "critical"}
            or failures >= 2
        )


def deterministic(task: str) -> Classification:
    return LocalDecisionEngine().decide(task)
