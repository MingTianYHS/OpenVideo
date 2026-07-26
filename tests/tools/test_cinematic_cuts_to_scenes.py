"""Regression tests for the CinematicRenderer cut-schema adapter (#358).

CinematicRenderer renders from ``props.scenes``. The canonical edit_decisions
artifact stores its timeline as ``cuts[]``. Before the adapter existed, the
cinematic families fell through to ``defaultProps.scenes = []`` and produced a
fixed 30s black video while reporting success.

These tests exercise the pure adapter plus the props assembly in
``_remotion_render`` — no Remotion CLI, no rendering.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tools.video.video_compose import VideoCompose


def _cut(cut_id, source, start, end, **extra):
    cut = {
        "id": cut_id,
        "source": source,
        "in_seconds": start,
        "out_seconds": end,
    }
    cut.update(extra)
    return cut


# ------------------------------------------------------------------
# _cuts_to_cinematic_scenes — pure adapter
# ------------------------------------------------------------------

def test_cuts_map_to_back_to_back_scenes():
    scenes = VideoCompose._cuts_to_cinematic_scenes([
        _cut("a", "one.mp4", 0.0, 4.0),
        _cut("b", "two.mp4", 2.0, 5.5),
    ])

    assert [s["id"] for s in scenes] == ["a", "b"]
    assert [s["kind"] for s in scenes] == ["video", "video"]
    assert [s["src"] for s in scenes] == ["one.mp4", "two.mp4"]

    # Laid end to end on the timeline...
    assert scenes[0]["startSeconds"] == 0.0
    assert scenes[0]["durationSeconds"] == 4.0
    assert scenes[1]["startSeconds"] == 4.0
    assert scenes[1]["durationSeconds"] == 3.5

    # ...each trimmed to its own in-point.
    assert scenes[0]["trimBeforeSeconds"] == 0.0
    assert scenes[1]["trimBeforeSeconds"] == 2.0


def test_fade_transitions_become_fade_frames():
    scenes = VideoCompose._cuts_to_cinematic_scenes([
        _cut("a", "one.mp4", 0, 2, transition_in="fade", transition_out="cut"),
        _cut("b", "two.mp4", 0, 2, transition_in="cut", transition_out="slow dissolve"),
    ])

    assert scenes[0]["fadeInFrames"] > 0
    assert scenes[0]["fadeOutFrames"] == 0
    assert scenes[1]["fadeInFrames"] == 0
    assert scenes[1]["fadeOutFrames"] > 0


def test_tone_defaults_to_neutral_and_is_forwarded():
    scenes = VideoCompose._cuts_to_cinematic_scenes([
        _cut("a", "one.mp4", 0, 2),
        _cut("b", "two.mp4", 0, 2, tone="cold"),
    ])

    assert scenes[0]["tone"] == "neutral"
    assert scenes[1]["tone"] == "cold"


@pytest.mark.parametrize("bad_cut", [
    {"id": "no-source", "in_seconds": 0, "out_seconds": 2},
    {"id": "zero-length", "source": "x.mp4", "in_seconds": 2, "out_seconds": 2},
    {"id": "negative", "source": "x.mp4", "in_seconds": 5, "out_seconds": 1},
    {"id": "unparseable", "source": "x.mp4", "in_seconds": "abc", "out_seconds": 3},
])
def test_unrenderable_cuts_are_dropped(bad_cut):
    assert VideoCompose._cuts_to_cinematic_scenes([bad_cut]) == []


def test_ids_are_synthesised_when_missing():
    scenes = VideoCompose._cuts_to_cinematic_scenes([
        {"source": "one.mp4", "in_seconds": 0, "out_seconds": 2},
        {"source": "two.mp4", "in_seconds": 0, "out_seconds": 2},
    ])
    assert [s["id"] for s in scenes] == ["scene-1", "scene-2"]


# ------------------------------------------------------------------
# _remotion_render — props assembly
# ------------------------------------------------------------------

REPO_ROOT = Path(__file__).resolve().parents[2]
COMPOSER_PUBLIC = REPO_ROOT / "remotion-composer" / "public"


@pytest.fixture(autouse=True)
def staged_cleanup():
    """Yield remotion-composer/public/ and remove anything the test staged into it.

    Staging writes into the real composer public dir (that is the point — it is
    what Remotion serves), so tests must not leave assets behind.
    """
    before = set(COMPOSER_PUBLIC.rglob("*")) if COMPOSER_PUBLIC.exists() else set()
    yield COMPOSER_PUBLIC
    if not COMPOSER_PUBLIC.exists():
        return
    created = sorted(set(COMPOSER_PUBLIC.rglob("*")) - before, reverse=True)
    for path in created:
        if path.is_file() or path.is_symlink():
            path.unlink()
        elif path.is_dir():
            path.rmdir()


@pytest.fixture()
def props_capture(monkeypatch, tmp_path):
    """Run _remotion_render up to the CLI call and hand back the props written."""
    captured = {}

    def fake_run_command(self, cmd, **kwargs):
        for arg in cmd:
            if arg.startswith("--props="):
                captured["props"] = json.loads(Path(arg.split("=", 1)[1]).read_text())
        # Materialise the expected output so _remotion_render's existence check passes.
        Path(captured["output_path"]).write_bytes(b"fake mp4")
        return None

    monkeypatch.setattr(VideoCompose, "run_command", fake_run_command, raising=False)

    def run(edit_decisions):
        out = tmp_path / "out.mp4"
        captured["output_path"] = str(out)
        tool = VideoCompose()
        tool._remotion_render({
            "edit_decisions": edit_decisions,
            "output_path": str(out),
        })
        return captured.get("props", {})

    return run


@pytest.mark.parametrize("family", sorted(VideoCompose.CUT_SCHEMA_CINEMATIC_FAMILIES))
def test_cinematic_families_get_scenes_from_cuts(props_capture, tmp_path, family):
    """#358: cut-schema props alone rendered 30s of black. Scenes must be derived."""
    clip = tmp_path / "clip.mp4"
    clip.write_bytes(b"fake mp4")

    props = props_capture({
        "renderer_family": family,
        "render_runtime": "remotion",
        "cuts": [_cut("sc1", str(clip), 0, 3)],
    })

    assert props.get("scenes"), f"{family} produced no scenes — would render black"
    assert props["scenes"][0]["durationSeconds"] == 3
    assert props["scenes"][0]["kind"] == "video"


def test_explicit_scenes_are_not_overwritten(props_capture, tmp_path):
    clip = tmp_path / "clip.mp4"
    clip.write_bytes(b"fake mp4")
    supplied = [{
        "id": "hand-authored",
        "kind": "video",
        "src": "already-staged.mp4",
        "startSeconds": 0,
        "durationSeconds": 1,
    }]

    props = props_capture({
        "renderer_family": "cinematic-trailer",
        "cuts": [_cut("sc1", str(clip), 0, 3)],
        "scenes": supplied,
    })

    assert props["scenes"] == supplied


def test_non_cinematic_families_get_no_scenes(props_capture, tmp_path):
    clip = tmp_path / "clip.mp4"
    clip.write_bytes(b"fake mp4")

    props = props_capture({
        "renderer_family": "explainer-data",
        "cuts": [_cut("sc1", str(clip), 0, 3)],
    })

    assert "scenes" not in props


# ------------------------------------------------------------------
# _remotion_render — local media staging
# ------------------------------------------------------------------

def test_local_sources_are_staged_not_file_uris(props_capture, tmp_path, staged_cleanup):
    """OffthreadVideo's compositor proxy rejects file://; props must stay relative."""
    clip = tmp_path / "clip.mp4"
    clip.write_bytes(b"fake mp4")

    props = props_capture({
        "renderer_family": "cinematic-trailer",
        "cuts": [_cut("sc1", str(clip), 0, 3)],
    })

    staged = props["cuts"][0]["source"]
    assert not staged.startswith("file://"), "file:// is rejected by the compositor proxy"
    assert not Path(staged).is_absolute()
    assert staged.startswith("openmontage_assets/")
    assert (staged_cleanup / staged).exists()


def test_remote_and_data_sources_pass_through(props_capture):
    props = props_capture({
        "renderer_family": "cinematic-trailer",
        "cuts": [
            _cut("a", "https://example.com/a.mp4", 0, 2),
            _cut("b", "data:video/mp4;base64,AAAA", 0, 2),
        ],
    })

    assert props["cuts"][0]["source"] == "https://example.com/a.mp4"
    assert props["cuts"][1]["source"] == "data:video/mp4;base64,AAAA"


def test_missing_local_file_is_left_untouched(props_capture, tmp_path):
    """Unresolvable sources belong to the validation gate, not the staging helper."""
    missing = tmp_path / "nope.mp4"

    props = props_capture({
        "renderer_family": "cinematic-trailer",
        "cuts": [_cut("sc1", str(missing), 0, 3)],
    })

    assert props["cuts"][0]["source"] == str(missing)
