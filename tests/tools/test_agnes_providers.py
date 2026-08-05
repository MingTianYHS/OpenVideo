"""Contract, payload, and dual-region tests for Agnes media providers."""

from __future__ import annotations

import base64

import pytest

from lib.providers.agnes import (
    AgnesClient,
    agnes_base_url_for_region,
    normalize_agnes_region,
)
from tools.base_tool import ToolStatus
from tools.graphics.agnes_image import AgnesImage
from tools.video.agnes_video import AgnesVideo


class TestAgnesRegions:
    def test_region_defaults_to_global_for_backward_compatibility(self, monkeypatch):
        monkeypatch.delenv("AGNES_REGION", raising=False)
        assert normalize_agnes_region() == "global"

    @pytest.mark.parametrize(
        ("value", "expected"),
        [("cn", "cn"), ("china", "cn"), ("global", "global"), ("international", "global")],
    )
    def test_region_aliases(self, value, expected):
        assert normalize_agnes_region(value) == expected

    def test_region_endpoints(self, monkeypatch):
        for name in ("AGNES_BASE_URL", "AGNES_CN_BASE_URL", "AGNES_GLOBAL_BASE_URL"):
            monkeypatch.delenv(name, raising=False)
        assert agnes_base_url_for_region("cn") == "https://api.agnes-ai.cn"
        assert agnes_base_url_for_region("global") == "https://apihub.agnes-ai.com"

    def test_trailing_v1_is_normalized(self, monkeypatch):
        monkeypatch.setenv("AGNES_CN_BASE_URL", "https://api.agnes-ai.cn/v1")
        assert agnes_base_url_for_region("cn") == "https://api.agnes-ai.cn"

    def test_region_specific_keys(self, monkeypatch):
        monkeypatch.setenv("AGNES_CN_API_KEY", "cn-key")
        monkeypatch.setenv("AGNES_GLOBAL_API_KEY", "global-key")
        cn = AgnesClient(region="cn")
        global_client = AgnesClient(region="global")
        assert cn.api_key == "cn-key"
        assert cn.base_url == "https://api.agnes-ai.cn"
        assert global_client.api_key == "global-key"
        assert global_client.base_url == "https://apihub.agnes-ai.com"


class TestAgnesImage:
    def test_contract(self):
        tool = AgnesImage()
        assert tool.provider == "agnes"
        assert tool.capability == "image_generation"
        assert tool.supports["regions"] == ["cn", "global"]
        assert tool.input_schema["properties"]["region"]["enum"] == ["cn", "global"]

    def test_status_uses_either_region_key(self, monkeypatch):
        for name in ("AGNES_API_KEY", "AGNES_CN_API_KEY", "AGNES_GLOBAL_API_KEY"):
            monkeypatch.delenv(name, raising=False)
        assert AgnesImage().get_status() == ToolStatus.UNAVAILABLE
        monkeypatch.setenv("AGNES_CN_API_KEY", "test-key")
        assert AgnesImage().get_status() == ToolStatus.AVAILABLE

    def test_text_to_image_payload(self):
        payload = AgnesImage()._build_payload(
            {"prompt": "cinematic skyline", "resolution": "2k", "aspect_ratio": "16:9"}
        )
        assert payload["model"] == "agnes-image-2.1-flash"
        assert payload["size"] == "2K"
        assert payload["extra_body"] == {"response_format": "url"}

    def test_edit_payload_places_images_in_extra_body(self, tmp_path):
        source = tmp_path / "source.png"
        source.write_bytes(b"image-bytes")
        payload = AgnesImage()._build_payload({
            "prompt": "preserve composition and relight",
            "generation_mode": "edit",
            "image_path": str(source),
            "image_urls": ["https://example.com/reference.png"],
            "return_base64": True,
        })
        images = payload["extra_body"]["image"]
        assert images[0].startswith("data:image/png;base64,")
        assert base64.b64decode(images[0].split(",", 1)[1]) == b"image-bytes"
        assert images[1] == "https://example.com/reference.png"
        assert payload["extra_body"]["response_format"] == "b64_json"


class TestAgnesVideo:
    def test_contract(self):
        tool = AgnesVideo()
        assert tool.provider == "agnes"
        assert tool.capability == "video_generation"
        assert tool.supports["regions"] == ["cn", "global"]

    @pytest.mark.parametrize(
        ("duration", "expected_frames"), [(3, 73), (5, 121), (10, 241), (30, 441)]
    )
    def test_duration_normalization(self, duration, expected_frames):
        frames = AgnesVideo._normalize_num_frames(duration, 24)
        assert frames == expected_frames
        assert (frames - 1) % 8 == 0

    def test_text_to_video_payload(self):
        payload = AgnesVideo()._build_payload({
            "prompt": "slow cinematic tracking shot",
            "duration": 5,
            "frame_rate": 24,
            "resolution": "720p",
            "aspect_ratio": "16:9",
            "seed": 42,
        })
        assert payload["model"] == "agnes-video-v2.0"
        assert payload["num_frames"] == 121
        assert payload["width"] == 1280
        assert payload["height"] == 720
        assert payload["seed"] == 42

    def test_keyframe_payload(self):
        payload = AgnesVideo()._build_payload({
            "prompt": "smooth transition",
            "operation": "reference_to_video",
            "reference_image_urls": [
                "https://example.com/start.png", "https://example.com/end.png"
            ],
        })
        assert payload["extra_body"]["mode"] == "keyframes"
