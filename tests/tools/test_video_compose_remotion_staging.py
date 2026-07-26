"""Integration: video_compose Remotion path stages into project-scoped public dir."""

from __future__ import annotations

import json
import uuid
from pathlib import Path

import pytest

from tools.video.video_compose import VideoCompose

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
COMPOSER_DIR = REPO_ROOT / "remotion-composer"


@pytest.mark.skipif(
    not COMPOSER_DIR.exists() or not (COMPOSER_DIR / "node_modules").exists(),
    reason="remotion-composer not installed",
)
def test_remotion_render_stages_into_project_public_dir(monkeypatch, tmp_path):
    slug = f"pytest-staging-{uuid.uuid4().hex[:8]}"
    project_dir = tmp_path / "projects" / slug
    renders = project_dir / "renders"
    renders.mkdir(parents=True)

    img = project_dir / "assets" / "frame.jpg"
    narr = project_dir / "assets" / "narration.mp3"
    img.parent.mkdir(parents=True)
    img.write_bytes(b"\xff\xd8\xff")
    narr.write_bytes(b"ID3")

    output_path = renders / "out.mp4"
    captured: dict = {}

    def fake_run_command(cmd, timeout=None, cwd=None):
        captured["cmd"] = list(cmd)
        for arg in cmd:
            if isinstance(arg, str) and arg.startswith("--props="):
                captured["props"] = json.loads(Path(arg.split("=", 1)[1]).read_text())
            if isinstance(arg, str) and arg.startswith("--public-dir="):
                captured["public_dir"] = arg.split("=", 1)[1]
        for arg in cmd:
            if isinstance(arg, str) and arg.endswith(".mp4"):
                Path(arg).write_bytes(b"\x00\x00\x00\x18ftypmp42")
                break

    monkeypatch.setattr(VideoCompose, "run_command", staticmethod(fake_run_command))

    import shutil

    monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/npx" if name == "npx" else None)

    tool = VideoCompose()
    result = tool._remotion_render(
        {
            "composition_data": {
                "version": "1.0",
                "render_runtime": "remotion",
                "renderer_family": "explainer-data",
                "cuts": [
                    {
                        "id": "c1",
                        "source": str(img),
                        "in_seconds": 0,
                        "out_seconds": 3,
                    }
                ],
                "audio": {
                    "narration": {"src": str(narr), "volume": 1.0},
                },
            },
            "output_path": str(output_path),
        }
    )

    project_public_parent = project_dir
    shared_public = COMPOSER_DIR / "public" / slug
    report_path = renders / ".remotion_asset_staging.json"

    assert result.success, result.error
    props = captured["props"]
    assert props["cuts"][0]["source"] == "frame.jpg"
    assert props["audio"]["narration"]["src"] == "narration.mp3"
    assert "--public-dir=" in " ".join(captured["cmd"])
    # Render-scoped: unique dir under projects/<slug>/, matching the staging contract.
    captured_public = Path(captured["public_dir"]).resolve()
    assert captured_public.parent == project_public_parent.resolve()
    assert captured_public.name.startswith("remotion-public-")
    # Media cleaned after render; shared composer public untouched.
    assert not captured_public.exists()
    assert not shared_public.exists()
    # Debug report survives cleanup.
    assert report_path.exists()
    report = json.loads(report_path.read_text())
    assert report["project_slug"] == slug
    assert len(report["staged"]) == 2
    assert result.data["remotion_asset_staging_report"] == str(report_path)


def test_remotion_render_pre_render_failure_cleans_staging(monkeypatch, tmp_path):
    """Regression: a staging/setup failure after the dir is created must not
    leave staged user media behind — the whole staging/setup/render lifetime
    is guarded by the finally that calls cleanup_staging_dir.
    """
    slug = f"pytest-fail-{uuid.uuid4().hex[:8]}"
    project_dir = tmp_path / "projects" / slug
    renders = project_dir / "renders"
    renders.mkdir(parents=True)

    img = project_dir / "assets" / "frame.jpg"
    img.parent.mkdir(parents=True)
    img.write_bytes(b"\xff\xd8\xff")

    output_path = renders / "out.mp4"

    # Track the public dir the tool resolved so we can assert it is cleaned.
    observed_public: dict = {}

    real_resolve = __import__(
        "lib.remotion_asset_staging", fromlist=["resolve_project_public_dir"]
    ).resolve_project_public_dir

    def spy_resolve(output, composition_data=None, **kw):
        resolved = real_resolve(output, composition_data, **kw)
        observed_public["dir"] = resolved
        return resolved

    monkeypatch.setattr(
        "lib.remotion_asset_staging.resolve_project_public_dir", spy_resolve
    )

    def boom_staging(*args, **kwargs):
        # Simulate staging creating the dir + copying a file, then failing
        # partway through (e.g. a corrupt source or disk error).
        public_dir = kwargs["public_dir"]
        public_dir.mkdir(parents=True, exist_ok=True)
        (public_dir / "partial.mp3").write_bytes(b"ID3")
        raise OSError("simulated staging failure mid-copy")

    monkeypatch.setattr(
        "lib.remotion_asset_staging.stage_local_assets_for_remotion", boom_staging
    )

    import shutil

    monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/npx" if name == "npx" else None)

    tool = VideoCompose()
    result = tool._remotion_render(
        {
            "composition_data": {
                "version": "1.0",
                "render_runtime": "remotion",
                "renderer_family": "explainer-data",
                "cuts": [
                    {
                        "id": "c1",
                        "source": str(img),
                        "in_seconds": 0,
                        "out_seconds": 3,
                    }
                ],
            },
            "output_path": str(output_path),
        }
    )

    assert not result.success
    assert "staging" in result.error.lower()
    # The render-scoped staging dir we created must have been cleaned up.
    assert "dir" in observed_public
    staged_dir = observed_public["dir"]
    assert staged_dir.name.startswith("remotion-public-")
    assert not staged_dir.exists()
    assert not (staged_dir / "partial.mp3").exists()
    # No stray staging dirs left under the project.
    leftovers = [p for p in project_dir.iterdir() if p.name.startswith("remotion-public")]
    assert leftovers == []
