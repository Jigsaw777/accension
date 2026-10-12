"""M2 real dependency chain, checks, human approval, receipts and restart."""

import importlib.util
from pathlib import Path


async def test_greeting_workbench_chain_real_engine_and_restart(tmp_path):
    source = Path(__file__).resolve().parents[1] / "scripts" / "demo_workbench.py"
    spec = importlib.util.spec_from_file_location("demo_workbench", source)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    result = await module.demo(tmp_path / "home", tmp_path / "repo")
    assert result["keys"] == ["DEMO-1", "DEMO-2", "DEMO-3"] and result["attempts"] == 3
    assert not result["paid_inference"] and result["restart"] == "keys/revisions/receipts preserved"


async def test_feature_demo_approved_batch_and_separate_review(tmp_path):
    source = Path(__file__).resolve().parents[1] / "scripts/demo_workbench.py"
    spec = importlib.util.spec_from_file_location("feature_demo_workbench", source)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    result = await module.demo(tmp_path / "home", tmp_path / "repo", feature=True)
    assert result["attempts"] == 3 and not result["paid_inference"]
    assert result["delivery"] == "human-approved batch with separate completion review"
