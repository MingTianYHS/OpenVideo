"""Contract and payload tests for Agnes Image 2.1 and Video 2.0 providers."""

from __future__ import annotations

import base64

import pytest

from tools.base_tool import ToolStatus
from tools.graphics.agnes_image import AgnesImage
from tools.video.agnes_video import AgnesVideo


class TestAgnesImage:
    def test_contract(self):
        tool = AgnesImage()
        assert tool.name == "agnes_image"
        assert tool.provider == "agnes"
        assert tool.capability == "image_generation"
        assert tool.supports["image_edit"] is True
        assert tool.input_schema["properties"]["model"]["enum"] == ["agnes-image-2.1-flash"]

    def test_status_uses_agnes_key(self, monkeypatch):
        monkeypatch.delenv("AGNES_API_KEY", raising=False)
        assert AgnesImage().get_status() == ToolStatus.UNAVAILABLE
        monkeypatch.setenv("AGNES_API_KEY", "test-key")
        assert AgnesImage().get_status() == ToolStatus.AVAILABLE

    def test_text_to_image_payload_uses_extra_body_response_format(self):
        payload = AgnesImage()._build_payload(
            {
                "prompt": "cinematic skyline",
                "resolution": "2k",
                "aspect_ratio": "16:9",
            }
        )
        assert payload["model"] == "agnes-image-2.1-flash"
        assert payload["size"] == "2K"
        assert payload["ratio"] == "16:9"
        assert payload["extra_body"] == {"response_format": "url"}
        assert "response_format" not in payload

    def test_edit_payload_places_images_in_extra_body(self, tmp_path):
        source = tmp_path / "source.png"
        source.write_bytes(b"image-bytes")
        payload = AgnesImage()._build_payload(
            {
                "prompt": "preserve composition and relight",
                "generation_mode": "edit",
                "image_path": str(source),
                "image_urls": ["https://example.com/reference.png"],
                "return_base64": True,
            }
        )
        images = payload["extra_body"]["image"]
        assert images[0].startswith("data:image/png;base64,")
        assert base64.b64decode(images[0].split(",", 1)[1]) == b"image-bytes"
        assert images[1] == "https://example.com/reference.png"
        assert payload["extra_body"]["response_format"] == "b64_json"

    def test_edit_requires_image(self):
        with pytest.raises(ValueError, match="requires an input image"):
            AgnesImage()._build_payload({"prompt": "edit", "generation_mode": "edit"})


class TestAgnesVideo:
    def test_contract(self):
        tool = AgnesVideo()
        assert tool.name == "agnes_video"
        assert tool.provider == "agnes"
        assert tool.capability == "video_generation"
        assert tool.supports["image_to_video"] is True
        assert tool.input_schema["properties"]["model"]["enum"] == ["agnes-video-v2.0"]

    @pytest.mark.parametrize(
        ("duration", "expected_frames"),
        [(3, 73), (5, 121), (10, 241), (30, 441)],
    )
    def test_duration_is_normalized_to_8n_plus_1(self, duration, expected_frames):
        frames = AgnesVideo._normalize_num_frames(duration, 24)
        assert frames == expected_frames
        assert (frames - 1) % 8 == 0
        assert frames <= 441

    def test_explicit_frames_are_normalized(self):
        assert AgnesVideo._normalize_num_frames(5, 24, explicit=120) == 113
        assert AgnesVideo._normalize_num_frames(5, 24, explicit=999) == 441

    def test_text_to_video_payload(self):
        payload = AgnesVideo()._build_payload(
            {
                "prompt": "slow cinematic tracking shot",
                "duration": 5,
                "frame_rate": 24,
                "resolution": "720p",
                "aspect_ratio": "16:9",
                "negative_prompt": "watermark",
                "seed": 42,
            }
        )
        assert payload["model"] == "agnes-video-v2.0"
        assert payload["num_frames"] == 121
        assert payload["width"] == 1280
        assert payload["height"] == 720
        assert payload["negative_prompt"] == "watermark"
        assert payload["seed"] == 42

    def test_image_to_video_requires_image(self):
        with pytest.raises(ValueError, match="requires image_url"):
            AgnesVideo()._build_payload(
                {"prompt": "animate", "operation": "image_to_video"}
            )

    def test_keyframe_payload(self):
        payload = AgnesVideo()._build_payload(
            {
                "prompt": "smooth transition",
                "operation": "reference_to_video",
                "reference_image_urls": [
                    "https://example.com/start.png",
                    "https://example.com/end.png",
                ],
            }
        )
        assert payload["extra_body"] == {
            "image": [
                "https://example.com/start.png",
                "https://example.com/end.png",
            ],
            "mode": "keyframes",
        }
