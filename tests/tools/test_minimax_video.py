"""Contract coverage for the first-party MiniMax video provider.

These tests mock ``requests`` via ``monkeypatch.setattr`` on the live module
so the shared ``requests`` import stays intact for every other test.
"""

from __future__ import annotations

import requests

from tools.base_tool import ToolStatus


class _FakeResponse:
    def __init__(self, *, json_data=None, content=b""):
        self._json = json_data or {}
        self.content = content

    def raise_for_status(self):
        return None

    def json(self):
        return self._json


def test_minimax_video_is_discovered_as_direct_provider():
    from tools.tool_registry import ToolRegistry

    registry = ToolRegistry()
    registry.discover()

    tool = registry.get("minimax_video")
    assert tool is not None
    assert tool.provider == "minimax"
    assert tool.capability == "video_generation"
    assert tool.dependencies == ["env:MINIMAX_API_KEY"]


def test_minimax_video_unavailable_without_key(monkeypatch):
    from tools.video.minimax_video import MiniMaxVideo

    monkeypatch.delenv("MINIMAX_API_KEY", raising=False)
    assert MiniMaxVideo().get_status() == ToolStatus.UNAVAILABLE

    monkeypatch.setenv("MINIMAX_API_KEY", "test-minimax-key")
    assert MiniMaxVideo().get_status() == ToolStatus.AVAILABLE


def test_minimax_video_region_routing(monkeypatch):
    from tools.video.minimax_video import MiniMaxVideo

    tool = MiniMaxVideo()
    monkeypatch.delenv("MINIMAX_BASE_URL", raising=False)
    monkeypatch.delenv("MINIMAX_REGION", raising=False)
    assert tool._base_url() == "https://api.minimax.io"

    monkeypatch.setenv("MINIMAX_REGION", "cn")
    assert tool._base_url() == "https://api.minimaxi.com"

    monkeypatch.setenv("MINIMAX_BASE_URL", "https://proxy.example.com/")
    assert tool._base_url() == "https://proxy.example.com"


def test_minimax_video_text_to_video_direct_api_contract(monkeypatch, tmp_path):
    from tools.video.minimax_video import MiniMaxVideo

    monkeypatch.setenv("MINIMAX_API_KEY", "test-minimax-key")
    monkeypatch.setenv("MINIMAX_REGION", "global")
    monkeypatch.setattr("time.sleep", lambda _s: None)

    calls = {}

    def fake_post(url, headers=None, json=None, timeout=None):
        calls["post_url"] = url
        calls["post_headers"] = headers
        calls["post_payload"] = json
        return _FakeResponse(json_data={"task_id": "task-123", "base_resp": {"status_code": 0}})

    def fake_get(url, headers=None, params=None, timeout=None):
        calls.setdefault("get", []).append((url, params))
        if url.endswith("/v1/query/video_generation"):
            return _FakeResponse(
                json_data={"status": "Success", "file_id": "file-9", "base_resp": {"status_code": 0}}
            )
        if url.endswith("/v1/files/retrieve"):
            return _FakeResponse(
                json_data={"file": {"download_url": "https://cdn.example/out.mp4"}, "base_resp": {"status_code": 0}}
            )
        return _FakeResponse(content=b"fake mp4 bytes")

    monkeypatch.setattr(requests, "post", fake_post)
    monkeypatch.setattr(requests, "get", fake_get)

    output_path = tmp_path / "clip.mp4"
    result = MiniMaxVideo().execute(
        {
            "prompt": "A calm product shot, slow dolly-in",
            "model": "MiniMax-Hailuo-2.3",
            "duration": 6,
            "resolution": "1080P",
            "output_path": str(output_path),
        }
    )

    assert result.success, result.error
    # Submits to the global direct endpoint with Bearer auth.
    assert calls["post_url"] == "https://api.minimax.io/v1/video_generation"
    assert calls["post_headers"]["Authorization"] == "Bearer test-minimax-key"
    # First-party request contract fields.
    assert calls["post_payload"]["model"] == "MiniMax-Hailuo-2.3"
    assert calls["post_payload"]["prompt"].startswith("A calm product shot")
    assert calls["post_payload"]["duration"] == 6
    assert calls["post_payload"]["resolution"] == "1080P"
    # Poll then retrieve then download.
    get_urls = [u for u, _ in calls["get"]]
    assert "https://api.minimax.io/v1/query/video_generation" in get_urls
    assert "https://api.minimax.io/v1/files/retrieve" in get_urls
    assert output_path.read_bytes() == b"fake mp4 bytes"
    assert result.data["task_id"] == "task-123"
    assert result.data["file_id"] == "file-9"
    assert result.model == "MiniMax-Hailuo-2.3"


def test_minimax_video_image_to_video_requires_first_frame(monkeypatch):
    from tools.video.minimax_video import MiniMaxVideo

    monkeypatch.setenv("MINIMAX_API_KEY", "test-minimax-key")
    result = MiniMaxVideo().execute({"prompt": "n/a", "operation": "image_to_video"})
    assert not result.success
    assert "first_frame_image" in result.error


def test_minimax_video_surfaces_base_resp_error(monkeypatch):
    from tools.video.minimax_video import MiniMaxVideo

    monkeypatch.setenv("MINIMAX_API_KEY", "test-minimax-key")

    def fake_post(url, headers=None, json=None, timeout=None):
        return _FakeResponse(json_data={"base_resp": {"status_code": 1004, "status_msg": "auth failed"}})

    monkeypatch.setattr(requests, "post", fake_post)

    result = MiniMaxVideo().execute({"prompt": "hi", "model": "MiniMax-Hailuo-2.3"})
    assert not result.success
    assert "1004" in result.error
