"""Agnes Video 2.0 text-to-video, image-to-video, and keyframe generation."""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any

from lib.providers.agnes import AgnesClient, configured_agnes_regions
from tools.base_tool import (
    BaseTool, Determinism, ExecutionMode, ResourceProfile, RetryPolicy,
    ToolResult, ToolRuntime, ToolStability, ToolStatus, ToolTier,
)


_RESOLUTION_DIMENSIONS = {
    "480p": {
        "16:9": (832, 448), "9:16": (448, 832), "1:1": (640, 640),
        "4:3": (768, 576), "3:4": (576, 768),
    },
    "720p": {
        "16:9": (1280, 720), "9:16": (720, 1280), "1:1": (1024, 1024),
        "4:3": (1152, 864), "3:4": (864, 1152),
    },
    "1080p": {
        "16:9": (1920, 1080), "9:16": (1080, 1920), "1:1": (1536, 1536),
        "4:3": (1440, 1080), "3:4": (1080, 1440),
    },
}


class AgnesVideo(BaseTool):
    name = "agnes_video"
    version = "0.2.0"
    tier = ToolTier.GENERATE
    capability = "video_generation"
    provider = "agnes"
    stability = ToolStability.BETA
    execution_mode = ExecutionMode.SYNC
    determinism = Determinism.SEEDED
    runtime = ToolRuntime.API

    dependencies = []
    install_instructions = (
        "Choose AGNES_REGION=cn or AGNES_REGION=global, then set the matching "
        "AGNES_CN_API_KEY or AGNES_GLOBAL_API_KEY. AGNES_API_KEY remains supported."
    )
    agent_skills = ["ai-video-gen"]

    capabilities = ["text_to_video", "image_to_video", "keyframe_video"]
    supports = {
        "text_to_video": True,
        "image_to_video": True,
        "reference_to_video": True,
        "multiple_reference_images": True,
        "negative_prompt": True,
        "seed": True,
        "frame_control": True,
        "regions": ["cn", "global"],
        "automatic_region_failover": False,
    }
    best_for = [
        "text-to-video and image-to-video generation",
        "keyframe transitions between multiple images",
        "seeded clips with frame-rate and duration control",
    ]
    not_good_for = ["offline generation", "clips longer than about 18 seconds"]
    fallback_tools = ["grok_video", "runway_video", "kling_video", "veo_video"]

    input_schema = {
        "type": "object",
        "required": ["prompt"],
        "properties": {
            "prompt": {"type": "string"},
            "region": {
                "type": "string", "enum": ["cn", "global"],
                "description": "Agnes API region. Overrides AGNES_REGION for this call.",
            },
            "operation": {
                "type": "string",
                "enum": ["text_to_video", "image_to_video", "reference_to_video", "keyframes"],
                "default": "text_to_video",
            },
            "model": {
                "type": "string", "enum": ["agnes-video-v2.0"],
                "default": "agnes-video-v2.0",
            },
            "duration": {"type": ["integer", "string"], "default": 5},
            "aspect_ratio": {
                "type": "string", "enum": ["16:9", "9:16", "1:1", "4:3", "3:4"],
                "default": "16:9",
            },
            "resolution": {
                "type": "string", "enum": ["480p", "720p", "1080p"], "default": "720p",
            },
            "width": {"type": "integer"},
            "height": {"type": "integer"},
            "num_frames": {"type": "integer", "maximum": 441},
            "frame_rate": {"type": "number", "minimum": 1, "maximum": 60, "default": 24},
            "num_inference_steps": {"type": "integer"},
            "seed": {"type": "integer"},
            "negative_prompt": {"type": "string"},
            "image_url": {"type": "string"},
            "reference_image_url": {"type": "string"},
            "reference_image_urls": {"type": "array", "items": {"type": "string"}},
            "output_path": {"type": "string"},
            "poll_interval_seconds": {"type": "number", "minimum": 2, "default": 5},
            "timeout_seconds": {"type": "integer", "minimum": 30, "default": 900},
        },
    }

    resource_profile = ResourceProfile(
        cpu_cores=1, ram_mb=512, vram_mb=0, disk_mb=1000, network_required=True
    )
    retry_policy = RetryPolicy(
        max_retries=2, retryable_errors=["rate_limit", "timeout", "server_error"]
    )
    idempotency_key_fields = ["prompt", "operation", "duration", "seed", "aspect_ratio", "region"]
    side_effects = ["writes video file to output_path", "calls Agnes AI video API"]
    user_visible_verification = ["Watch generated clip and verify duration, motion, and prompt fidelity"]

    def get_status(self) -> ToolStatus:
        return ToolStatus.AVAILABLE if configured_agnes_regions() else ToolStatus.UNAVAILABLE

    @staticmethod
    def _normalize_num_frames(duration: Any, frame_rate: float, explicit: Any = None) -> int:
        requested = (
            max(1, min(441, int(explicit))) if explicit is not None
            else max(1, min(441, round(float(duration or 5) * frame_rate)))
        )
        return min(441, max(1, ((requested - 1 + 7) // 8) * 8 + 1))

    @staticmethod
    def _dimensions(inputs: dict[str, Any]) -> tuple[int, int]:
        if inputs.get("width") and inputs.get("height"):
            return int(inputs["width"]), int(inputs["height"])
        resolution = str(inputs.get("resolution", "720p")).lower()
        ratio = str(inputs.get("aspect_ratio", "16:9"))
        choices = _RESOLUTION_DIMENSIONS.get(resolution, _RESOLUTION_DIMENSIONS["720p"])
        return choices.get(ratio, choices["16:9"])

    def estimate_cost(self, inputs: dict[str, Any]) -> float:
        frame_rate = float(inputs.get("frame_rate", 24))
        frames = self._normalize_num_frames(
            inputs.get("duration", 5), frame_rate, inputs.get("num_frames")
        )
        return float(os.environ.get("AGNES_VIDEO_COST_PER_SECOND", "0")) * (frames / frame_rate)

    def _build_payload(self, inputs: dict[str, Any]) -> dict[str, Any]:
        frame_rate = float(inputs.get("frame_rate", 24))
        if frame_rate < 1 or frame_rate > 60:
            raise ValueError("frame_rate must be between 1 and 60")
        num_frames = self._normalize_num_frames(
            inputs.get("duration", 5), frame_rate, inputs.get("num_frames")
        )
        width, height = self._dimensions(inputs)
        payload: dict[str, Any] = {
            "model": "agnes-video-v2.0",
            "prompt": inputs["prompt"],
            "width": width,
            "height": height,
            "num_frames": num_frames,
            "frame_rate": frame_rate,
        }
        for key in ("num_inference_steps", "seed", "negative_prompt"):
            if inputs.get(key) is not None:
                payload[key] = inputs[key]
        operation = inputs.get("operation", "text_to_video")
        if operation == "image_to_video":
            image = inputs.get("image_url") or inputs.get("reference_image_url")
            if not image:
                raise ValueError("image_to_video requires image_url or reference_image_url")
            payload["image"] = image
        elif operation in {"reference_to_video", "keyframes"}:
            images = list(inputs.get("reference_image_urls") or [])
            if len(images) < 2:
                raise ValueError("keyframe video requires at least two reference_image_urls")
            payload["extra_body"] = {"image": images, "mode": "keyframes"}
        return payload

    @staticmethod
    def _completed_url(data: dict[str, Any]) -> str | None:
        metadata = data.get("metadata") or {}
        return metadata.get("url") if isinstance(metadata, dict) else None

    def _poll_result(
        self,
        client: AgnesClient,
        *,
        video_id: str | None,
        task_id: str | None,
        timeout_seconds: int,
        poll_interval: float,
    ) -> dict[str, Any]:
        deadline = time.time() + timeout_seconds
        last_data: dict[str, Any] = {}
        while time.time() < deadline:
            if video_id:
                data = client.get_json(
                    "/agnesapi",
                    params={"video_id": video_id, "model_name": "agnes-video-v2.0"},
                    timeout=30,
                )
            elif task_id:
                data = client.get_json(f"/v1/videos/{task_id}", timeout=30)
            else:
                raise RuntimeError("Agnes video response is missing video_id and task_id")
            last_data = data
            status = str(data.get("status", "")).lower()
            if status == "completed":
                return data
            if status == "failed":
                raise RuntimeError(f"Agnes video generation failed: {data.get('error') or 'unknown error'}")
            time.sleep(min(poll_interval, max(0.0, deadline - time.time())))
        raise TimeoutError(
            f"Agnes {client.region} video generation timed out after {timeout_seconds}s; "
            f"last status was {last_data.get('status', 'unknown')}"
        )

    def execute(self, inputs: dict[str, Any]) -> ToolResult:
        import requests
        from tools.video._shared import probe_output

        start = time.time()
        try:
            client = AgnesClient(region=inputs.get("region"))
            payload = self._build_payload(inputs)
            submitted = client.post_json("/v1/videos", payload, timeout=60)
            video_id = submitted.get("video_id")
            task_id = submitted.get("task_id") or submitted.get("id")
            result = self._poll_result(
                client,
                video_id=str(video_id) if video_id else None,
                task_id=str(task_id) if task_id else None,
                timeout_seconds=int(inputs.get("timeout_seconds", 900)),
                poll_interval=float(inputs.get("poll_interval_seconds", 5)),
            )
            video_url = self._completed_url(result)
            if not video_url:
                raise RuntimeError("Completed Agnes video response is missing metadata.url")
            download = requests.get(video_url, timeout=(15, 300))
            download.raise_for_status()
            output_path = Path(inputs.get("output_path", "agnes_video_output.mp4"))
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_bytes(download.content)
        except Exception as exc:
            return ToolResult(success=False, error=f"Agnes video generation failed: {exc}")

        probed = probe_output(output_path)
        actual_seconds = float(result.get("seconds") or probed.get("duration_seconds") or 0)
        return ToolResult(
            success=True,
            data={
                "provider": "agnes",
                "region": client.region,
                "base_url": client.base_url,
                "model": "agnes-video-v2.0",
                "prompt": inputs["prompt"],
                "operation": inputs.get("operation", "text_to_video"),
                "task_id": task_id,
                "video_id": video_id,
                "output": str(output_path),
                "output_path": str(output_path),
                "format": "mp4",
                "seconds": actual_seconds,
                "size": result.get("size"),
                "requested_num_frames": payload["num_frames"],
                "requested_frame_rate": payload["frame_rate"],
                **probed,
            },
            artifacts=[str(output_path)],
            cost_usd=self.estimate_cost(inputs),
            duration_seconds=round(time.time() - start, 2),
            seed=inputs.get("seed"),
            model="agnes-video-v2.0",
        )
