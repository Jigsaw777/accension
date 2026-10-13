"""Documentation gates must work without private files in the public checkout."""

import importlib.util
import json
import subprocess
from pathlib import Path


def checker():
    spec = importlib.util.spec_from_file_location("check_docs", Path(__file__).parents[1] / "scripts/check_docs.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_public_links_anchors_images_and_cli(tmp_path):
    subprocess.run(["git", "init", "--quiet", str(tmp_path)], check=True)
    (tmp_path / ".gitignore").write_text("private/\n")
    (tmp_path / "private").mkdir()
    (tmp_path / "private/notes.md").write_text("# Private\n")
    (tmp_path / "guide.md").write_text("# Guide\n## Repeat\n## Repeat\n")
    (tmp_path / "board.png").write_bytes(b"fixture")
    readme = tmp_path / "README.md"
    readme.write_text(
        "# Start\n[Guide](guide.md#repeat-1)\n![Workbench board with demo tasks](board.png)\n```sh\naccs doctor\n```\n"
    )
    module = checker()
    assert module.check(tmp_path) == []
    readme.write_text(
        "# Start\n[Private](private/notes.md)\n[Broken](guide.md#missing)\n"
        "![Screenshot](board.png)\n![Workbench board with tasks](board.png)\n"
        "```sh\naccs removed-command\n```\n"
    )
    failures = "\n".join(module.check(tmp_path))
    assert "ignored/untracked public link" in failures
    assert "missing heading anchor" in failures
    assert "descriptive alt text" in failures
    assert "duplicate image" in failures
    assert "stale CLI example" in failures


def test_heading_ids_ignore_code_and_keep_unicode():
    module = checker()
    assert module.anchors("## A **bold** title!\n```text\n## hidden\n```\n## Café\n") == {
        "a-bold-title",
        "café",
    }


def test_manifest_and_product_version_mismatches(tmp_path):
    subprocess.run(["git", "init", "--quiet", str(tmp_path)], check=True)
    metadata = tmp_path / "src/local_ai_router"
    metadata.mkdir(parents=True)
    (metadata / "__init__.py").write_text('__version__ = "1.1.0"\n')
    (tmp_path / "README.md").write_text("**Version 1.0.0**\n")
    gallery = tmp_path / "docs/guides/screenshots"
    gallery.mkdir(parents=True)
    (gallery / "board.png").write_bytes(b"changed")
    (gallery / "capture-manifest.json").write_text(json.dumps({"images": [{"file": "board.png", "sha256": "wrong"}]}))
    failures = "\n".join(checker().check(tmp_path))
    assert "product version differs" in failures
    assert "changed image board.png" in failures
