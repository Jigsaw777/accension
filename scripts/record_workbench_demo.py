"""Record the real isolated Workbench, then encode and verify silent launch videos.

Requires .[browser], Playwright Chromium, FFmpeg/ffprobe and an installed TrueType font.
No production profile, external inference, desktop capture or UI replacement is used.
"""

import argparse
import hashlib
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DISCLOSURE = "Demo fixture / mock inference / real local tests"
SCENES = [
    ("hook", 5, "AI writes code. Who tracks the work?", "Accension - Local-first AI engineering Workbench"),
    ("requirements", 8, "Turn requirements into a reviewable PRD", "Requirements approval is separate from execution"),
    ("breakdown", 9, "Break work into dependent tasks", "Greeting, tests, documentation / completed fixture tasks"),
    ("scope", 4, "Choose eligible agents and approve execution", "Review allowed files and registered checks"),
    ("approval", 5, "Assignment does not start execution", "Review the current contract before approving"),
    ("evidence", 6, "Verify actual results", "Reviewing a completed attempt / real local tests"),
    ("review", 6, "An AI success message is not completion", "Inspect evidence, then approve completion separately"),
    ("closing", 8, "Accension 1.1.0", "Open source / Self-hosted / Local-first"),
]
LAYOUTS = {"16x9": (1440, 675, 1920, 1080), "linkedin-4x5": (960, 1000, 1080, 1350)}


def run(argv, **kwargs):
    return subprocess.run(
        [str(arg) for arg in argv], check=True, capture_output=True, text=True, encoding="utf-8", **kwargs
    ).stdout


def sha256(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def probe(path, ffprobe):
    return json.loads(run([ffprobe, "-v", "error", "-show_streams", "-show_format", "-of", "json", path]))


def capture(output, name, width, height, ffprobe):
    import httpx
    from playwright.sync_api import expect, sync_playwright

    # A fresh service for each aspect ratio keeps approval actions reproducible.
    with tempfile.TemporaryDirectory(prefix="accension-film-") as directory:
        base = Path(directory)
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        url = f"http://127.0.0.1:{port}"
        options = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}
        with (base / "server.log").open("w", encoding="utf-8") as log:
            process = subprocess.Popen(
                [
                    sys.executable,
                    str(ROOT / "scripts/capture_docs_screenshots.py"),
                    "--serve",
                    str(base / "home"),
                    "--port",
                    str(port),
                ],
                cwd=ROOT,
                stdout=log,
                stderr=log,
                **options,
            )
            try:
                with httpx.Client(trust_env=False) as client:
                    deadline = time.monotonic() + 90
                    while True:
                        try:
                            if client.get(url + "/health").is_success:
                                break
                        except httpx.ConnectError:
                            pass
                        if process.poll() is not None or time.monotonic() > deadline:
                            raise RuntimeError("Isolated service failed: " + (base / "server.log").read_text()[-2000:])
                        time.sleep(0.1)
                with sync_playwright() as pw:
                    browser = pw.chromium.launch(headless=True)
                    context = browser.new_context(
                        viewport={"width": width, "height": height},
                        record_video_dir=str(base / "recording"),
                        record_video_size={"width": width, "height": height},
                    )
                    # Even accidental external browser requests are refused.
                    context.route(
                        "**/*",
                        lambda route: route.continue_() if route.request.url.startswith(url + "/") else route.abort(),
                    )
                    page = context.new_page()
                    video = page.video
                    errors, scenes = [], []
                    page.on("pageerror", lambda error: errors.append(str(error)))
                    page.add_init_script("localStorage.setItem('accension-theme','dark')")
                    page.goto(url + "/ui/workbench")
                    expect(page.locator("#connection")).to_contain_text("Connected")
                    page.locator("#project").select_option(label="Greeting feature (demo)")
                    page.evaluate("document.fonts.ready")

                    def focus(locator):
                        expect(locator).to_be_visible()
                        locator.evaluate("node => scrollTo(0, node.getBoundingClientRect().top + scrollY - 155)")

                    def hold(index, action=None):
                        key, duration, title, subtitle = SCENES[index]
                        assert not errors, errors
                        # Fail closed on personal paths or credentials in rendered text.
                        visible = page.locator("body").inner_text()
                        assert not re.search(r"[A-Z]:[\\/]Users[\\/]|Bearer\s+[A-Za-z0-9]|sk-[A-Za-z0-9]{16}", visible)
                        start = time.monotonic()
                        page.wait_for_timeout(600)
                        page.screenshot(path=str(output / f"source-{name}-{key}.png"))
                        if action:
                            page.wait_for_timeout(1800)
                            action()
                        page.wait_for_timeout(max(0, duration + 0.8 - (time.monotonic() - start)) * 1000)
                        page.screenshot(path=str(output / f"source-{name}-{key}-end.png"))
                        scenes.append(
                            {"key": key, "clock": start, "duration": duration, "title": title, "subtitle": subtitle}
                        )
                        print(f"Captured {name}: {key}", flush=True)

                    def board():
                        page.get_by_role("button", name="Board", exact=True).click()
                        expect(page.locator("#content")).to_contain_text("Verify the greeting contract")
                        page.evaluate("scrollTo(0,0)")
                        page.locator(".board").evaluate("node => node.scrollLeft = node.scrollWidth - node.clientWidth")
                        focus(page.locator(".column[data-state='DONE']"))

                    board()
                    hold(0)
                    page.get_by_role("button", name="PRDs", exact=True).click()
                    page.get_by_role(
                        "button", name=re.compile(r"^Greeting function, tests and documentation ·")
                    ).click()
                    focus(page.get_by_label("PRD title", exact=True))

                    def requirements():
                        focus(page.get_by_label("functional requirements text 1", exact=True))
                        page.wait_for_timeout(2200)
                        focus(page.get_by_label("acceptance criteria text 1", exact=True))

                    hold(1, requirements)
                    board()
                    for title in ("Greeting function", "Greeting tests", "Usage documentation"):
                        expect(page.get_by_role("button", name=title, exact=True)).to_be_in_viewport()
                    hold(2)
                    page.get_by_role("button", name="PRDs", exact=True).click()
                    page.get_by_role(
                        "button", name=re.compile(r"^Greeting function, tests and documentation ·")
                    ).click()
                    page.get_by_role("button", name="Review imported feature", exact=True).click()
                    focus(page.get_by_role("heading", name="Review feature execution", exact=True))
                    hold(3)
                    board()
                    page.get_by_role("button", name="Verify the greeting contract", exact=True).click()
                    page.get_by_role("button", name="ready", exact=True).click()
                    page.get_by_role("button", name="Preview assignment", exact=True).click()
                    assign = page.locator("#detail").get_by_role("button", name=re.compile(r"^Assign [a-f0-9]"))
                    expect(assign).to_have_count(1)
                    assign.click()
                    page.get_by_role("button", name="Review execution contract", exact=True).click()
                    focus(page.get_by_role("button", name="Approve execution", exact=True))
                    hold(4)
                    page.get_by_role("button", name="Approve execution", exact=True).click()
                    page.get_by_role("button", name="Review execution contract", exact=True).click()
                    page.get_by_role("button", name="Run approved item", exact=True).click()
                    item = page.locator(".column[data-state='REVIEW']").get_by_role(
                        "button", name="Verify the greeting contract", exact=True
                    )
                    expect(item).to_be_visible(timeout=30000)
                    item.click()
                    evidence = page.get_by_role("heading", name="Evidence for completion approval", exact=True)
                    focus(evidence)
                    panel = evidence.locator("xpath=following-sibling::pre[1]")
                    text = panel.inner_text()
                    assert '"passed": true' in text and '"exit_code": 0' in text and '"receipt_hash"' in text
                    (output / f"evidence-{name}.json").write_text(text + "\n", encoding="utf-8")
                    hold(5, lambda: panel.evaluate("node => node.scrollTop = 230"))
                    focus(page.get_by_role("button", name="approve_completion", exact=True))
                    page.get_by_label("Command reason", exact=True).fill(
                        "Fixture operator reviewed passing checks and artifacts"
                    )

                    def complete():
                        page.get_by_role("button", name="approve_completion", exact=True).click()
                        expect(page.locator("#detail")).to_contain_text("DONE")
                        page.get_by_role("button", name="Overview", exact=True).click()
                        expect(page.locator("#content")).to_contain_text("DONE: 4")
                        page.evaluate("scrollTo(0,0)")

                    hold(6, complete)
                    page.get_by_role("button", name="Overview", exact=True).click()
                    page.evaluate("scrollTo(0,0)")
                    hold(7)
                    assert not errors, errors
                    version = browser.version
                    ended = time.monotonic()
                    context.close()
                    raw = output / f"raw-{name}.webm"
                    video.save_as(str(raw))
                    browser.close()
                length = float(probe(raw, ffprobe)["format"]["duration"])
                for scene in scenes:
                    scene["start"] = round(length - (ended - scene.pop("clock")) + 0.2, 3)
                    assert 0 <= scene["start"] < length - scene["duration"], (scene, length)
                return {
                    "raw": raw.name,
                    "sha256": sha256(raw),
                    "viewport": [width, height],
                    "chromium": version,
                    "scenes": scenes,
                }
            finally:
                # Windows venv launchers and the Workbench owner spawn children.
                if os.name == "nt":
                    subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"], capture_output=True, check=False)
                else:
                    process.terminate()
                try:
                    process.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=10)


def encode(output, capture_record, name, ffmpeg, font):
    _, _, width, height = LAYOUTS[name]
    raw = output / capture_record["raw"]
    if sha256(raw) != capture_record["sha256"]:
        raise ValueError("Raw capture hash mismatch")
    shutil.copyfile(font, output / "caption-font.ttf")
    clips = []
    for index, scene in enumerate(capture_record["scenes"]):
        title, subtitle = scene["title"], scene["subtitle"]
        if width == 1080 and len(title) > 38:
            words = title.split()
            pivot = min(range(1, len(words)), key=lambda n: abs(len(" ".join(words[:n])) - len(" ".join(words[n:]))))
            title = " ".join(words[:pivot]) + "\n" + " ".join(words[pivot:])
        for key, value in {
            "title": title,
            "subtitle": subtitle,
            "disclosure": DISCLOSURE,
            "url": "github.com/Jigsaw777/accension",
        }.items():
            # FFmpeg treats Windows CRLF as two line breaks in drawtext files.
            (output / f"{key}.txt").write_text(value, encoding="utf-8", newline="\n")
        ui_height = 900 if width == 1920 else 1125
        font_size = 38 if width == 1920 else 30
        filters = [
            "setpts=PTS-STARTPTS",
            f"scale={width}:{ui_height}:flags=lanczos",
            f"pad={width}:{height}:0:120:color=0x101b18",
            "setsar=1",
            "fps=30",
        ]
        for key, size, y, color in [
            ("title", font_size, "12", "0xe9fff5"),
            ("subtitle", 23 if width == 1920 else 21, "88", "0x9be4be"),
            ("disclosure", 21, str(height - 36), "0xd6e7df"),
        ]:
            filters.append(
                f"drawtext=fontfile=caption-font.ttf:textfile={key}.txt:expansion=none:"
                f"fontsize={size}:fontcolor={color}:x=(w-text_w)/2:y={y}"
            )
        if scene["key"] == "closing":
            filters.append(
                "drawtext=fontfile=caption-font.ttf:textfile=url.txt:expansion=none:"
                "fontsize=32:fontcolor=white:box=1:boxcolor=0x101b18@0.95:boxborderw=16:"
                "x=(w-text_w)/2:y=h-100"
            )
        clip = f"clip-{name}-{index}.mp4"
        run(
            [
                ffmpeg,
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-ss",
                scene["start"],
                "-i",
                raw.name,
                "-frames:v",
                scene["duration"] * 30,
                "-vf",
                ",".join(filters),
                "-an",
                "-c:v",
                "libx264",
                "-preset",
                "fast",
                "-crf",
                "21",
                "-pix_fmt",
                "yuv420p",
                "-threads",
                "4",
                clip,
            ],
            cwd=output,
        )
        clips.append(clip)
    concat = output / f"concat-{name}.txt"
    concat.write_text("".join(f"file '{clip}'\n" for clip in clips), encoding="utf-8")
    target = output / f"demo-{name}.mp4"
    run(
        [
            ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "concat",
            "-safe",
            "1",
            "-i",
            concat.name,
            "-c",
            "copy",
            "-movflags",
            "+faststart",
            target.name,
        ],
        cwd=output,
    )
    return target


def verify(path, width, height, ffmpeg, ffprobe):
    metadata = probe(path, ffprobe)
    stream = metadata["streams"][0]
    duration = float(metadata["format"]["duration"])
    assert (stream["width"], stream["height"]) == (width, height)
    assert stream["codec_name"] == "h264" and stream["pix_fmt"] == "yuv420p"
    assert stream["avg_frame_rate"] == "30/1" and 50.9 <= duration <= 51.2
    run([ffmpeg, "-v", "error", "-xerror", "-i", path, "-f", "null", "-"])
    result = subprocess.run(
        [
            str(ffmpeg),
            "-hide_banner",
            "-i",
            str(path),
            "-vf",
            "blackdetect=d=0.15:pix_th=0.05:pic_th=0.99",
            "-an",
            "-f",
            "null",
            "-",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    assert "black_start:" not in result.stderr
    return {
        "file": path.name,
        "bytes": path.stat().st_size,
        "sha256": sha256(path),
        "duration": duration,
        "width": width,
        "height": height,
        "codec": stream["codec_name"],
        "pixel_format": stream["pix_fmt"],
        "fps": 30,
        "full_decode": "passed",
        "black_frames": "none at 99% / 0.15s threshold",
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / "dist/media/accension-1.1.0-launch")
    parser.add_argument("--ffmpeg", default="ffmpeg")
    parser.add_argument("--ffprobe", default="ffprobe")
    parser.add_argument("--font", type=Path, required=True, help="Installed TrueType font; never downloaded")
    parser.add_argument("--encode-only", action="store_true", help="Re-encode hash-verified captures in --output")
    args = parser.parse_args()
    output = args.output.resolve()
    if not args.font.is_file():
        parser.error("--font must be an existing TrueType font")
    ffmpeg, ffprobe = shutil.which(args.ffmpeg), shutil.which(args.ffprobe)
    if not ffmpeg or not ffprobe:
        parser.error("FFmpeg and ffprobe are required; supply their executable paths")
    if not args.encode_only:
        output.mkdir(parents=True, exist_ok=False)
        sources = [
            "scripts/record_workbench_demo.py",
            "scripts/demo_workbench.py",
            "scripts/capture_docs_screenshots.py",
        ]
        sources += [
            str(p.relative_to(ROOT)).replace("\\", "/")
            for p in (ROOT / "src/local_ai_router/static").glob("*")
            if p.is_file()
        ]
        manifest = {
            "product_version": "1.1.0",
            "captured_at": datetime.now(timezone.utc).isoformat(),
            "source_commit": run(["git", "rev-parse", "HEAD"], cwd=ROOT).strip(),
            "source_sha256": {
                p: hashlib.sha256((ROOT / p).read_bytes().replace(b"\r\n", b"\n")).hexdigest() for p in sources
            },
            "method": "Playwright Chromium record_video; two responsive browser viewports; FFmpeg trims/letterboxes/captions",
            "fixture": "capture_docs_screenshots.serve / demo_workbench --feature; isolated temporary home/repository",
            "inference": "deterministic mock only; provider discovery disabled; no paid inference",
            "tests": "python -m unittest discover -v; named/blank greeting cases; persisted receipt hashes",
            "disclosure": DISCLOSURE,
            "story": "First three tasks pre-executed and reviewed by fixture operator. Fourth task assigned, approved, executed and completion-reviewed through real UI. Execution wait omitted; evidence explicitly reviewed after execution.",
            "captures": {},
        }
        for name, (width, height, _, _) in LAYOUTS.items():
            manifest["captures"][name] = capture(output, name, width, height, ffprobe)
            (output / "capture.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    else:
        manifest = json.loads((output / "capture.json").read_text(encoding="utf-8"))
    manifest["ffmpeg"] = run([ffmpeg, "-version"]).splitlines()[0]
    manifest["encoding_source_sha256"] = sha256(Path(__file__).resolve())
    manifest["font"] = {"name": args.font.name, "sha256": sha256(args.font)}
    manifest["outputs"] = []
    for name, (_, _, width, height) in LAYOUTS.items():
        target = encode(output, manifest["captures"][name], name, ffmpeg, args.font)
        manifest["outputs"].append(verify(target, width, height, ffmpeg, ffprobe))
        offset = 0
        for scene in SCENES:
            run(
                [
                    ffmpeg,
                    "-v",
                    "error",
                    "-y",
                    "-ss",
                    offset + 2,
                    "-i",
                    target,
                    "-frames:v",
                    "1",
                    output / f"review-{name}-{scene[0]}.png",
                ]
            )
            offset += scene[1]
        print(f"Encoded and decoded {target.name}", flush=True)
    primary = output / "demo-16x9.mp4"
    run([ffmpeg, "-v", "error", "-y", "-ss", "2", "-i", primary, "-frames:v", "1", output / "thumbnail.png"])
    run(
        [
            ffmpeg,
            "-v",
            "error",
            "-y",
            "-i",
            primary,
            "-t",
            "8",
            "-filter_complex",
            "fps=6,scale=640:-1:flags=lanczos,split[a][b];[a]palettegen=max_colors=64[p];[b][p]paletteuse=dither=bayer",
            "-loop",
            "0",
            output / "accension-demo-preview.gif",
        ]
    )
    run([ffmpeg, "-v", "error", "-xerror", "-i", output / "accension-demo-preview.gif", "-f", "null", "-"])
    manifest["auxiliary_outputs"] = []
    for name in ("thumbnail.png", "accension-demo-preview.gif"):
        path = output / name
        metadata = probe(path, ffprobe)
        stream = metadata["streams"][0]
        manifest["auxiliary_outputs"].append(
            {
                "file": name,
                "bytes": path.stat().st_size,
                "sha256": sha256(path),
                "width": stream["width"],
                "height": stream["height"],
                "duration": metadata["format"].get("duration", "still"),
            }
        )
    notes = (ROOT / "docs/media/README.md").read_text(encoding="utf-8")
    notes = notes.replace("(accension-demo.mp4)", "(demo-16x9.mp4)")
    notes = notes.replace("(accension-demo-manifest.json)", "(media-manifest.json)")
    gallery = os.path.relpath(ROOT / "docs/guides/screenshots/feature-showcase.md", output).replace("\\", "/")
    notes = notes.replace("(../guides/screenshots/feature-showcase.md", f"({gallery}")
    notes += "\n\nPublic screenshot source links (captured application revision):\n"
    for name in ("feature-board-dark-desktop.png", "feature-prd-light-desktop.png", "feature-review-light-desktop.png"):
        notes += (
            f"https://github.com/Jigsaw777/accension/blob/{manifest['source_commit']}/docs/guides/screenshots/{name}\n"
        )
    (output / "README.txt").write_text(notes, encoding="utf-8")
    manifest["visual_review"] = (
        "Pending inspection of review-*.png and playback; automated decoding alone is insufficient"
    )
    (output / "media-manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(manifest["outputs"], indent=2))


if __name__ == "__main__":
    main()
