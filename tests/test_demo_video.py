"""Reject corrupt source captures and incompatible publication encodings."""

import importlib.util
from pathlib import Path

import pytest


@pytest.fixture
def recorder():
    spec = importlib.util.spec_from_file_location(
        "record_workbench_demo", Path(__file__).parents[1] / "scripts/record_workbench_demo.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_changed_capture_is_rejected_before_encoding(recorder, tmp_path):
    raw = tmp_path / "raw.webm"
    raw.write_bytes(b"original recording")
    record = {"raw": raw.name, "sha256": recorder.sha256(raw)}
    raw.write_bytes(b"changed recording")
    with pytest.raises(ValueError, match="hash mismatch"):
        recorder.encode(tmp_path, record, "16x9", "not-invoked", tmp_path / "missing-font.ttf")
    assert list(tmp_path.iterdir()) == [raw]


@pytest.mark.parametrize("field,value", [("codec_name", "vp9"), ("pix_fmt", "yuv444p"), ("width", 640)])
def test_incompatible_mp4_is_rejected(recorder, monkeypatch, tmp_path, field, value):
    stream = {"width": 1920, "height": 1080, "codec_name": "h264", "pix_fmt": "yuv420p", "avg_frame_rate": "30/1"}
    stream[field] = value
    monkeypatch.setattr(recorder, "probe", lambda *args: {"streams": [stream], "format": {"duration": "51"}})
    with pytest.raises(AssertionError):
        recorder.verify(tmp_path / "demo.mp4", 1920, 1080, "not-invoked", "not-invoked")
