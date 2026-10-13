"""Media artifacts contain unchanged verified PNG bytes."""

import importlib.util
import json
import struct
import zipfile
from pathlib import Path

import pytest


def test_packaging_bytes_and_failed_verification_preserve_output(tmp_path):
    spec = importlib.util.spec_from_file_location(
        "capture_docs", Path(__file__).parents[1] / "scripts/capture_docs_screenshots.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    source = tmp_path / "source"
    source.mkdir()
    # Header-only inputs exercise packaging; actual captures are checked by
    # Playwright and visual inspection, rather than claimed by this unit test.
    data = b"\x89PNG\r\n\x1a\n" + struct.pack(">I", 13) + b"IHDR" + struct.pack(">II", 1440, 1000)
    for _, name in module.MEDIA:
        (source / name).write_bytes(data)
    (source / "capture-manifest.json").write_text(
        json.dumps({"images": [module.image_record(source / name) for _, name in module.MEDIA]})
    )
    result = module.package(source, tmp_path / "media")
    archive = Path(result["archive"])
    before = archive.read_bytes()
    with zipfile.ZipFile(archive) as bundle:
        assert len(bundle.namelist()) == 7
        for name, _ in module.MEDIA:
            assert bundle.read(name) == data
        assert "mock inference" in bundle.read("README.txt").decode()
    (source / module.MEDIA[0][1]).write_bytes(data + b"changed")
    with pytest.raises(ValueError, match="Manifest mismatch"):
        module.package(source, tmp_path / "media")
    assert archive.read_bytes() == before
