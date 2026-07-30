"""Tests for the Vertex AI backend path in veo_video.

The Google backend used to hard-reject Vertex-mode genai clients even though
the SDK supports Veo on Vertex (and the tool already mapped the Vertex model
name). It also unconditionally called `client.files.download`, which only
exists on the Gemini Developer API backend — on Vertex the video bytes come
back inline on the asset.
"""

import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))


class _FakeVideoAsset:
    def __init__(self, video_bytes):
        self.video_bytes = video_bytes
        self.uri = None

    def save(self, path):
        Path(path).write_bytes(self.video_bytes or b"")


class _FakeGeneratedVideo:
    def __init__(self, video_bytes):
        self.video = _FakeVideoAsset(video_bytes)


class _FakeOperation:
    def __init__(self, video_bytes):
        self.done = True
        self.error = None
        self.response = type(
            "R", (), {"generated_videos": [_FakeGeneratedVideo(video_bytes)]}
        )()


class _FakeFiles:
    def download(self, file=None):
        raise AssertionError(
            "files.download must not be called on the Vertex backend"
        )


def _make_vertex_client(video_bytes):
    class _FakeModels:
        def generate_videos(self, model=None, prompt=None, image=None, config=None):
            return _FakeOperation(video_bytes)

    class _FakeClient:
        vertexai = True
        models = _FakeModels()
        files = _FakeFiles()

    return _FakeClient()


@pytest.fixture
def veo_tool(monkeypatch):
    monkeypatch.setenv("GOOGLE_API_KEY", "test-key")
    from tools.video.veo_video import VeoVideo

    return VeoVideo()


def _patch_client(monkeypatch, client):
    import tools.google_credentials as gc

    monkeypatch.setattr(
        gc, "get_genai_client", lambda http_options=None, location=None: client
    )


def test_vertex_client_is_accepted_and_saves_inline_bytes(
    veo_tool, monkeypatch, tmp_path
):
    _patch_client(monkeypatch, _make_vertex_client(b"VIDEO_BYTES"))
    out = tmp_path / "clip.mp4"

    result = veo_tool.execute(
        {"prompt": "a grassland", "backend": "google", "output_path": str(out)}
    )

    # A Vertex-mode client must not be rejected, and the video must reach disk
    # without touching the developer-API-only Files service.
    assert result.success, result.error
    assert out.read_bytes() == b"VIDEO_BYTES"


def test_vertex_response_without_inline_bytes_is_a_clear_error(
    veo_tool, monkeypatch, tmp_path
):
    _patch_client(monkeypatch, _make_vertex_client(None))

    result = veo_tool.execute(
        {
            "prompt": "a grassland",
            "backend": "google",
            "output_path": str(tmp_path / "clip.mp4"),
        }
    )

    assert not result.success
    assert "without inline bytes" in result.error
