"""Seedance 2.0 (ByteDance) video generation via fal.ai API.

Best for cinematic clips with native audio, director-level camera control,
and lip-sync from quoted dialogue in prompts.
"""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any

from tools.base_tool import (
    BaseTool,
    Determinism,
    ExecutionMode,
    ResourceProfile,
    RetryPolicy,
    ToolResult,
    ToolRuntime,
    ToolStability,
    ToolStatus,
    ToolTier,
)


class SeedanceVideo(BaseTool):
    name = "seedance_video"
    version = "0.2.0"
    tier = ToolTier.GENERATE
    capability = "video_generation"
    provider = "seedance"
    stability = ToolStability.BETA
    execution_mode = ExecutionMode.SYNC
    determinism = Determinism.STOCHASTIC
    runtime = ToolRuntime.API

    dependencies = []
    install_instructions = (
        "Set FAL_KEY to your fal.ai API key.\n"
        "  Get one at https://fal.ai/dashboard/keys"
    )
    agent_skills = ["seedance-2-0", "ai-video-gen"]

    capabilities = ["text_to_video", "image_to_video", "reference_to_video"]
    supports = {
        "text_to_video": True,
        "image_to_video": True,
        "reference_to_video": True,
        "multiple_reference_images": True,
        "reference_image": True,
        "native_audio": True,
        "cinematic_quality": True,
        "camera_direction": True,
        "lip_sync": True,
        "multi_shot": True,
        "aspect_ratio": True,
        "seed": True,
    }
    best_for = [
        "preferred premium video gen when FAL_KEY is available",
        "cinematic trailers, teasers, and high-fidelity clips with native synchronized audio",
        "director-level camera control and multi-shot editing in a single generation",
        "lip-sync from quoted dialogue in prompts",
        "reference-conditioned generation (up to 9 images + 3 video clips + 3 audio clips)",
        "consistent character identity across shots",
    ]
    not_good_for = ["offline generation", "budget-constrained projects"]
    fallback_tools = ["veo_video", "kling_video", "minimax_video"]
    # Premium model — beat out "experimental stability" baseline. The scoring
    # engine reads quality_score directly when present (see lib/scoring.py).
    quality_score = 0.95

    input_schema = {
        "type": "object",
        "required": ["prompt"],
        "properties": {
            "prompt": {"type": "string"},
            "operation": {
                "type": "string",
                "enum": ["text_to_video", "image_to_video", "reference_to_video"],
                "default": "text_to_video",
            },
            "model_variant": {
                "type": "string",
                "enum": ["standard", "fast"],
                "default": "standard",
                "description": "standard = highest quality, fast = lower latency and cost",
            },
            "duration": {
                "type": "string",
                "enum": ["auto", "4", "5", "6", "7", "8", "9", "10", "11", "12", "13", "14", "15"],
                "default": "5",
                "description": "Duration in seconds. 'auto' lets the model decide.",
            },
            "aspect_ratio": {
                "type": "string",
                "enum": ["auto", "21:9", "16:9", "4:3", "1:1", "3:4", "9:16"],
                "default": "16:9",
            },
            "resolution": {
                "type": "string",
                "enum": ["480p", "720p"],
                "default": "720p",
            },
            "generate_audio": {
                "type": "boolean",
                "default": True,
                "description": "Generate synchronized audio (speech, SFX, ambient)",
            },
            "image_url": {
                "type": "string",
                "description": "Start frame image URL for image_to_video (jpg, png, webp)",
            },
            "image_path": {
                "type": "string",
                "description": "Local start-frame path for image_to_video. Auto-uploaded to fal.ai storage.",
            },
            "end_image_url": {
                "type": "string",
                "description": "Optional end frame URL for image_to_video",
            },
            "reference_image_urls": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Up to 9 reference image URLs for reference_to_video (identity / wardrobe / setting / style anchors).",
            },
            "reference_image_paths": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Local reference image paths for reference_to_video. Auto-uploaded to fal.ai storage.",
            },
            "reference_video_urls": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Up to 3 reference video clip URLs for reference_to_video (motion / camera / pacing anchors).",
            },
            "reference_audio_urls": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Up to 3 reference audio clip URLs for reference_to_video (voice / music / ambience anchors).",
            },
            "seed": {
                "type": "integer",
                "description": "Optional seed for reproducibility",
            },
            "output_path": {"type": "string"},
        },
    }

    resource_profile = ResourceProfile(
        cpu_cores=1, ram_mb=512, vram_mb=0, disk_mb=500, network_required=True
    )
    retry_policy = RetryPolicy(max_retries=2, retryable_errors=["rate_limit", "timeout"])
    idempotency_key_fields = ["prompt", "model_variant", "operation", "duration", "seed"]
    side_effects = ["writes video file to output_path", "calls fal.ai API"]
    user_visible_verification = [
        "Watch generated clip for motion coherence, audio sync, and visual quality"
    ]

    REFERENCE_OUTBOUND_FIELDS = (
        "prompt",
        "duration",
        "aspect_ratio",
        "resolution",
        "generate_audio",
        "seed",
        "image_urls",
        "video_urls",
        "audio_urls",
    )
    TRACE_HEADER_NAMES = frozenset(
        {
            "cf-ray",
            "date",
            "server-timing",
            "traceparent",
            "x-amzn-trace-id",
            "x-cloud-trace-context",
            "x-fal-request-id",
            "x-request-id",
        }
    )

    def _get_api_key(self) -> str | None:
        return os.environ.get("FAL_KEY") or os.environ.get("FAL_AI_API_KEY")

    def get_status(self) -> ToolStatus:
        if self._get_api_key():
            return ToolStatus.AVAILABLE
        return ToolStatus.UNAVAILABLE

    def estimate_cost(self, inputs: dict[str, Any]) -> float:
        variant = inputs.get("model_variant", "standard")
        duration = inputs.get("duration", "5")
        secs = 5 if duration == "auto" else int(duration)
        rate = 0.2419 if variant == "fast" else 0.3034
        return round(rate * secs, 2)

    def estimate_runtime(self, inputs: dict[str, Any]) -> float:
        variant = inputs.get("model_variant", "standard")
        return 60.0 if variant == "fast" else 120.0

    @classmethod
    def _trace_headers(cls, headers: Any) -> dict[str, str]:
        return {
            str(key): str(value)
            for key, value in dict(headers or {}).items()
            if str(key).lower() in cls.TRACE_HEADER_NAMES
            or "request-id" in str(key).lower()
            or "trace" in str(key).lower()
        }

    @classmethod
    def _http_failure_result(
        cls,
        *,
        exc: Exception,
        phase: str,
        model_path: str,
        request_id: str | None,
        terminal_queue_status: str,
        started: float,
        outbound_payload: dict[str, Any],
    ) -> ToolResult:
        response = getattr(exc, "response", None)
        status_code = getattr(exc, "status_code", None)
        if status_code is None and response is not None:
            status_code = getattr(response, "status_code", None)
        response_body = ""
        if response is not None:
            try:
                response_body = response.text
            except Exception:
                response_body = repr(getattr(response, "content", b""))
        response_headers = cls._trace_headers(
            getattr(exc, "response_headers", None)
            or getattr(response, "headers", None)
        )
        elapsed = round(time.monotonic() - started, 2)
        diagnostics = {
            "provider": "seedance",
            "model": model_path,
            "phase": phase,
            "http_status": status_code,
            "response_body": response_body,
            "response_headers": response_headers,
            "fal_request_id": request_id,
            "terminal_queue_status": terminal_queue_status,
            "elapsed_processing_seconds": elapsed,
            "outbound_payload": outbound_payload,
        }
        return ToolResult(
            success=False,
            data=diagnostics,
            error=(
                f"Seedance 2.0 {phase} failed"
                f" with HTTP {status_code}: {response_body}"
            ),
            cost_usd=0.0,
            duration_seconds=elapsed,
            model=model_path,
        )

    def execute(self, inputs: dict[str, Any]) -> ToolResult:
        api_key = self._get_api_key()
        if not api_key:
            return ToolResult(
                success=False,
                error="FAL_KEY not set. " + self.install_instructions,
            )

        import fal_client
        import requests

        started = time.monotonic()
        operation = inputs.get("operation", "text_to_video")
        variant = inputs.get("model_variant", "standard")
        operation_path = operation.replace("_", "-")

        if variant == "fast":
            model_path = f"bytedance/seedance-2.0/fast/{operation_path}"
        else:
            model_path = f"bytedance/seedance-2.0/{operation_path}"

        payload: dict[str, Any] = {"prompt": inputs["prompt"]}

        if inputs.get("duration"):
            payload["duration"] = inputs["duration"]
        if inputs.get("aspect_ratio"):
            payload["aspect_ratio"] = inputs["aspect_ratio"]
        if inputs.get("resolution"):
            payload["resolution"] = inputs["resolution"]
        if "generate_audio" in inputs:
            payload["generate_audio"] = inputs["generate_audio"]
        if inputs.get("seed") is not None:
            payload["seed"] = inputs["seed"]

        if operation == "image_to_video":
            if inputs.get("image_url"):
                payload["image_url"] = inputs["image_url"]
            elif inputs.get("image_path"):
                from tools.video._shared import upload_image_fal
                payload["image_url"] = upload_image_fal(inputs["image_path"])
            if inputs.get("end_image_url"):
                payload["end_image_url"] = inputs["end_image_url"]

        if operation == "reference_to_video":
            ref_image_urls = list(inputs.get("reference_image_urls") or [])
            try:
                for local_path in inputs.get("reference_image_paths") or []:
                    from tools.video._shared import upload_image_fal
                    ref_image_urls.append(upload_image_fal(local_path))
            except Exception as exc:
                return ToolResult(
                    success=False,
                    error=f"Seedance 2.0 reference preparation failed: {exc}",
                )
            # Seedance 2.0 reference-to-video ceilings: 9 images + 3 video + 3 audio.
            if len(ref_image_urls) > 9:
                return ToolResult(
                    success=False,
                    error=f"Seedance 2.0 reference_to_video accepts at most 9 reference images; got {len(ref_image_urls)}",
                )
            ref_video_urls = list(inputs.get("reference_video_urls") or [])
            if len(ref_video_urls) > 3:
                return ToolResult(
                    success=False,
                    error=f"Seedance 2.0 reference_to_video accepts at most 3 reference videos; got {len(ref_video_urls)}",
                )
            ref_audio_urls = list(inputs.get("reference_audio_urls") or [])
            if len(ref_audio_urls) > 3:
                return ToolResult(
                    success=False,
                    error=f"Seedance 2.0 reference_to_video accepts at most 3 reference audio clips; got {len(ref_audio_urls)}",
                )
            if ref_image_urls:
                payload["image_urls"] = ref_image_urls
            if ref_video_urls:
                payload["video_urls"] = ref_video_urls
            if ref_audio_urls:
                payload["audio_urls"] = ref_audio_urls

            # Provider boundary: selector/repository inputs and deprecated
            # aliases must never cross into the Seedance request body.
            payload = {
                field: payload[field]
                for field in self.REFERENCE_OUTBOUND_FIELDS
                if field in payload
            }

        handle = None
        request_id = None
        terminal_queue_status = "NOT_SUBMITTED"
        try:
            handle = fal_client.submit(model_path, payload)
            request_id = getattr(handle, "request_id", None)
            terminal_queue_status = "SUBMITTED"
        except fal_client.FalClientHTTPError as exc:
            return self._http_failure_result(
                exc=exc,
                phase="submission",
                model_path=model_path,
                request_id=request_id,
                terminal_queue_status=terminal_queue_status,
                started=started,
                outbound_payload=payload,
            )
        except Exception as exc:
            elapsed = round(time.monotonic() - started, 2)
            return ToolResult(
                success=False,
                data={
                    "provider": "seedance",
                    "model": model_path,
                    "phase": "submission",
                    "fal_request_id": request_id,
                    "terminal_queue_status": terminal_queue_status,
                    "elapsed_processing_seconds": elapsed,
                    "outbound_payload": payload,
                },
                error=f"Seedance 2.0 submission failed: {exc}",
                cost_usd=0.0,
                duration_seconds=elapsed,
                model=model_path,
            )

        try:
            while True:
                status = handle.status(with_logs=True)
                terminal_queue_status = type(status).__name__.upper()
                if isinstance(status, fal_client.Completed):
                    status_error = getattr(status, "error", None)
                    if status_error:
                        error_type = getattr(status, "error_type", None)
                        elapsed = round(time.monotonic() - started, 2)
                        return ToolResult(
                            success=False,
                            data={
                                "provider": "seedance",
                                "model": model_path,
                                "phase": "polling",
                                "http_status": None,
                                "response_body": status_error,
                                "response_headers": {},
                                "fal_request_id": request_id,
                                "terminal_queue_status": "COMPLETED_WITH_ERROR",
                                "elapsed_processing_seconds": elapsed,
                                "outbound_payload": payload,
                                "error_type": error_type,
                            },
                            error=f"Seedance 2.0 completed with error: {status_error}",
                            cost_usd=0.0,
                            duration_seconds=elapsed,
                            model=model_path,
                        )
                    break
                time.sleep(5)
        except fal_client.FalClientHTTPError as exc:
            return self._http_failure_result(
                exc=exc,
                phase="polling",
                model_path=model_path,
                request_id=request_id,
                terminal_queue_status=terminal_queue_status,
                started=started,
                outbound_payload=payload,
            )

        try:
            data = handle.get()
        except fal_client.FalClientHTTPError as exc:
            return self._http_failure_result(
                exc=exc,
                phase="result",
                model_path=model_path,
                request_id=request_id,
                terminal_queue_status=terminal_queue_status,
                started=started,
                outbound_payload=payload,
            )

        try:
            video_url = data["video"]["url"]
            video_response = requests.get(video_url, timeout=120)
            video_response.raise_for_status()

            output_path = Path(inputs.get("output_path", "seedance_output.mp4"))
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_bytes(video_response.content)

        except Exception as exc:
            elapsed = round(time.monotonic() - started, 2)
            return ToolResult(
                success=False,
                data={
                    "provider": "seedance",
                    "model": model_path,
                    "phase": "download",
                    "fal_request_id": request_id,
                    "terminal_queue_status": terminal_queue_status,
                    "elapsed_processing_seconds": elapsed,
                    "outbound_payload": payload,
                },
                error=f"Seedance 2.0 result download failed: {exc}",
                cost_usd=0.0,
                duration_seconds=elapsed,
                model=model_path,
            )

        from tools.video._shared import probe_output

        probed = probe_output(output_path)
        return ToolResult(
            success=True,
            data={
                "provider": "seedance",
                "model": model_path,
                "prompt": inputs["prompt"],
                "operation": operation,
                "variant": variant,
                "aspect_ratio": inputs.get("aspect_ratio", "16:9"),
                "resolution": inputs.get("resolution", "720p"),
                "generate_audio": inputs.get("generate_audio", True),
                "seed": data.get("seed"),
                "output": str(output_path),
                "output_path": str(output_path),
                "format": "mp4",
                **probed,
            },
            artifacts=[str(output_path)],
            cost_usd=self.estimate_cost(inputs),
            duration_seconds=round(time.monotonic() - started, 2),
            model=model_path,
        )
