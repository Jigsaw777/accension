"""Deterministic no-cost fixture, never a fallback for real provider failures."""

import json

from .schema import Generation, Usage


def generate(messages, role):
    raw = messages[-1]["content"]
    data = json.loads(raw)
    if role == "planner":
        goal = data["goal"]
        if "greet" not in goal.lower():
            raise ValueError("Mock fixture only implements the documented greeting demo")
        result = {
            "goal": goal,
            "architecture_summary": "Pure greeting function, independent documentation and dependent tests.",
            "success_criteria": ["Greeting handles names and blank input"],
            "global_validation": ["tests"],
            "tasks": [
                {
                    "id": "greeting",
                    "title": "Greeting function",
                    "objective": "Implement greet(name)",
                    "acceptance_criteria": ["Strip whitespace; blank names become world"],
                    "expected_artifacts": ["greeting.py"],
                    "max_attempts": 2,
                },
                {
                    "id": "docs",
                    "title": "Usage documentation",
                    "objective": "Document greet",
                    "acceptance_criteria": ["Document function behavior"],
                    "expected_artifacts": ["USAGE.md"],
                },
                {
                    "id": "tests",
                    "title": "Greeting tests",
                    "objective": "Test greet",
                    "dependencies": ["greeting"],
                    "relevant_files": ["greeting.py"],
                    "acceptance_criteria": ["Test named and empty inputs"],
                    "expected_artifacts": ["test_greeting.py"],
                    "validation_commands": ["tests"],
                },
            ],
        }
    elif role == "reviewer":
        result = {"accepted": True, "findings": []}
    elif role == "arbiter":
        result = {
            "model_id": "mock-worker",
            "recommended_tier": 1,
            "planning_required": True,
            "reason_code": "LOCAL_SUFFICIENT",
        }
    elif role in {"executor", "repair"}:
        texts = {
            "greeting.py": 'def greet(name: str) -> str:\n    """Return a greeting, using world for blank input."""\n    return f"Hello, {name.strip() or \'world\'}!"\n',
            "USAGE.md": '# Greeting\n\n`greet(" Ada ")` returns `Hello, Ada!`. Blank names use `world`.\n',
            "test_greeting.py": 'import unittest\nfrom greeting import greet\n\nclass GreetingTests(unittest.TestCase):\n    def test_named(self):\n        self.assertEqual(greet(" Ada "), "Hello, Ada!")\n    def test_blank(self):\n        self.assertEqual(greet("  "), "Hello, world!")\n',
        }
        files = {f["path"]: f for f in data["files"]}
        result = {
            "status": "complete",
            "summary": data["task"]["title"],
            "files_changed": [
                {"path": p, "content": texts[p], "original_sha256": files[p]["original_sha256"]}
                for p in data["task"]["expected_artifacts"]
            ],
            "confidence": 1,
        }
    else:
        result = {"message": "Mock gateway response"}
    text = json.dumps(result)
    return Generation(text=text, usage=Usage(input_tokens=len(raw) // 4, output_tokens=len(text) // 4))
