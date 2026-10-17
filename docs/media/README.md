# Accension 1.1.0 launch media

[Complete demo](accension-demo.mp4) · [8-second preview](accension-demo-preview.gif) · [Verified manifest](accension-demo-manifest.json) · [Capture and reproduction guide](../guides/screenshots/feature-showcase.md#recorded-product-demo)

## Maintainer upload checklist

1. Play `dist/media/accension-1.1.0-launch/demo-linkedin-4x5.mp4` and inspect its captions. Upload this **1080 × 1350** version to LinkedIn.
2. Choose `dist/media/accension-1.1.0-launch/thumbnail.png` as the cover image. It is an actual Workbench capture with a title.
3. Use `demo-16x9.mp4` for general video sharing. The README links to the identical `docs/media/accension-demo.mp4`; commit the documentation/media changes before sharing that repository link.
4. Keep the disclosure below with the post. Review platform-specific copy before submitting it.
5. Apply GitHub About changes only after explicit maintainer approval. Recording and README changes do not update repository metadata.

The MP4s are silent, captioned, 51 seconds, H.264/yuv420p, 30 fps, with fast-start metadata. The general version is 1920 × 1080. The GIF is 640 × 360, 8 seconds and 6 fps. Check hashes against `media-manifest.json` in the launch folder. Generated `dist/` files are ignored and are not published downloads.

## Suggested social caption

AI can write code. Who tracks the work?

Accension 1.1.0 brings requirements, dependent tasks, agent routing, approved execution and evidence-based completion review into a local-first engineering Workbench.

Open source, self-hosted, no hosted Accension account required.

[Explore Accension](https://github.com/Jigsaw777/accension)

Demo disclosure: isolated synthetic greeting project, deterministic mock inference and a scripted fixture operator; real local tests, artifacts and receipts. Completed attempts are labeled as recorded results. Demo costs and metrics are not production benchmarks. Local storage does not make optional cloud inference offline or private.

## Proposed GitHub About values — pending approval

Description:

> Local-first AI engineering Workbench for PRDs, agent routing, dependent tasks, verified execution and review.

Topics:

`ai-agents`, `agent-orchestration`, `developer-tools`, `local-first`, `self-hosted`, `python`, `mcp`, `coding-agents`

These values are prepared for review. No GitHub settings or social posts were changed.

## Screenshots and regeneration

The [gallery](../guides/screenshots/feature-showcase.md) links the verified board, PRD, approval, completion-evidence, agent and mobile captures. All use synthetic data. Raw browser videos and representative source/output frames stay in the local launch directory for inspection.

Follow the [recording instructions](../guides/screenshots/feature-showcase.md#reproduce-and-inspect-the-video). Use a fresh output directory, Playwright Chromium, FFmpeg/ffprobe and an installed font. Each capture uses its own disposable profile and repository. Review generated frames and playback before copying the verified general MP4, GIF and manifest here. No automatic upload, release publication or social posting occurs.
