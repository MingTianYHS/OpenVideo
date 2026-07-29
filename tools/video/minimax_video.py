"""MiniMax (Hailuo) video generation via the official MiniMax API.

Calls the first-party MiniMax video endpoints directly with ``MINIMAX_API_KEY``
(Bearer auth), covering both the global (``api.minimax.io``) and CN
(``api.minimaxi.com``) regions. Rewards prompt craft — follows camera
directions well and produces high-texture footage.
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

# Direct MiniMax API hosts per region. The global region is the default; set
# MINIMAX_REGION=cn to route to the mainland China host.
REGION_BASE_URLS = {
    "global": "https://api.minimax.io",
    "cn": "https://api.minimaxi.com",
}
DEFAULT_REGION = "global"

# Current first-party model slugs (Hailuo 2.3 family plus prior generations).
MODELS = [
    "MiniMax-Hailuo-2.3",
    "MiniMax-Hailuo-2.3-Fast",
    "MiniMax-Hailuo-02",
    "T2V-01-Director",
    "T2V-01",
    "I2V-01-Director",
    "I2V-01-live",
    "I2V-01",
]
DEFAULT_MODEL = "MiniMax-Hailuo-2.3"

# In-progress poll states returned by the query endpoint. Anything outside the
# terminal Success/Fail values means the task is still running.
_TERMINAL_SUCCESS = "Success"
_TERMINAL_FAIL = "Fail"


class MiniMaxVideo(BaseTool):
    name = "minimax_video"
    version = "0.2.0"
    tier = ToolTier.GENERATE
    capability = "video_generation"
    provider = "minimax"
    stability = ToolStability.EXPERIMENTAL
    execution_mode = ExecutionMode.SYNC
    determinism = Determinism.STOCHASTIC
    runtime = ToolRuntime.API

    dependencies = ["env:MINIMAX_API_KEY"]
    install_instructions = (
        "Set MINIMAX_API_KEY to your MiniMax API key.\n"
        "  Get one at https://platform.minimax.io/ (global) or "
        "https://platform.minimaxi.com/ (CN).\n"
        "  Optionally set MINIMAX_REGION=cn to route to the mainland China host, "
        "or MINIMAX_BASE_URL to override the endpoint entirely."
    )
    agent_skills = ["ai-video-gen"]

    capabilities = ["text_to_video", "image_to_video"]
    supports = {
        "text_to_video": True,
        "image_to_video": True,
        "camera_direction": True,
    }
    best_for = [
        "prompt-following with camera directions (framing, motion, composition)",
        "high-texture footage with minimal hallucination",
        "direct first-party API access with your own MiniMax quota",
    ]
    not_good_for = ["offline generation", "very long clips"]
    fallback_tools = ["kling_video", "veo_video", "wan_video"]

    input_schema = {
        "type": "object",
        "required": ["prompt"],
        "properties": {
            "prompt": {"type": "string"},
            "operation": {
                "type": "string",
                "enum": ["text_to_video", "image_to_video"],
                "default": "text_to_video",
            },
            "model": {
                "type": "string",
                "enum": MODELS,
                "default": DEFAULT_MODEL,
            },
            "first_frame_image": {
                "type": "string",
                "description": (
                    "First-frame image for image_to_video: a public URL or a "
                    "data URI (data:image/...;base64,<payload>)."
                ),
            },
            "prompt_optimizer": {"type": "boolean", "default": True},
            "fast_pretreatment": {"type": "boolean"},
            "duration": {"type": "integer", "description": "Clip length in seconds."},
            "resolution": {"type": "string", "description": "Output resolution, e.g. 768P or 1080P."},
            "callback_url": {"type": "string"},
            "output_path": {"type": "string"},
        },
    }

    resource_profile = ResourceProfile(
        cpu_cores=1, ram_mb=512, vram_mb=0, disk_mb=500, network_required=True
    )
    retry_policy = RetryPolicy(max_retries=2, retryable_errors=["rate_limit", "timeout"])
    idempotency_key_fields = [
        "prompt",
        "model",
        "operation",
        "first_frame_image",
        "prompt_optimizer",
        "fast_pretreatment",
        "duration",
        "resolution",
    ]
    side_effects = ["writes video file to output_path", "calls MiniMax video API"]
    user_visible_verification = ["Watch generated clip for motion coherence and prompt adherence"]

    def _get_api_key(self) -> str | None:
        return os.environ.get("MINIMAX_API_KEY")

    def _base_url(self) -> str:
        override = os.environ.get("MINIMAX_BASE_URL")
        if override:
            return override.rstrip("/")
        region = os.environ.get("MINIMAX_REGION", DEFAULT_REGION).strip().lower()
        return REGION_BASE_URLS.get(region, REGION_BASE_URLS[DEFAULT_REGION])

    def get_status(self) -> ToolStatus:
        if self._get_api_key():
            return ToolStatus.AVAILABLE
        return ToolStatus.UNAVAILABLE

    def estimate_cost(self, inputs: dict[str, Any]) -> float:
        model = inputs.get("model", DEFAULT_MODEL)
        if "Fast" in model:
            return 0.08
        return 0.15

    def estimate_runtime(self, inputs: dict[str, Any]) -> float:
        model = inputs.get("model", DEFAULT_MODEL)
        if "Fast" in model:
            return 30.0
        return 60.0

    @staticmethod
    def _base_resp_error(payload: dict[str, Any]) -> str | None:
        """Return an error string when MiniMax base_resp signals failure."""
        base_resp = payload.get("base_resp") or {}
        status_code = base_resp.get("status_code")
        if status_code in (None, 0):
            return None
        status_msg = base_resp.get("status_msg", "")
        return f"MiniMax API error {status_code}: {status_msg}".strip()

    def execute(self, inputs: dict[str, Any]) -> ToolResult:
        api_key = self._get_api_key()
        if not api_key:
            return ToolResult(
                success=False,
                error="MINIMAX_API_KEY not set. " + self.install_instructions,
            )

        import requests

        start = time.time()
        base_url = self._base_url()
        operation = inputs.get("operation", "text_to_video")
        model = inputs.get("model", DEFAULT_MODEL)

        payload: dict[str, Any] = {"model": model}
        if operation == "image_to_video":
            first_frame = inputs.get("first_frame_image")
            if not first_frame:
                return ToolResult(
                    success=False,
                    error="image_to_video requires 'first_frame_image'.",
                )
            payload["first_frame_image"] = first_frame
            if inputs.get("prompt"):
                payload["prompt"] = inputs["prompt"]
        else:
            if not inputs.get("prompt"):
                return ToolResult(
                    success=False,
                    error="text_to_video requires 'prompt'.",
                )
            payload["prompt"] = inputs["prompt"]

        # Optional request fields, passed through only when supplied.
        if "prompt_optimizer" in inputs:
            payload["prompt_optimizer"] = inputs["prompt_optimizer"]
        for field in ("fast_pretreatment", "duration", "resolution", "callback_url"):
            if inputs.get(field) is not None:
                payload[field] = inputs[field]

        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }

        def _redact(text: str) -> str:
            return text.replace(api_key, "***") if api_key else text

        try:
            # Submit generation task.
            submit_resp = requests.post(
                f"{base_url}/v1/video_generation",
                headers=headers,
                json=payload,
                timeout=30,
            )
            submit_resp.raise_for_status()
            submit_data = submit_resp.json()
            base_err = self._base_resp_error(submit_data)
            if base_err:
                return ToolResult(success=False, error=base_err)
            task_id = submit_data.get("task_id")
            if not task_id:
                return ToolResult(
                    success=False,
                    error="MiniMax API did not return a task_id.",
                )

            # Poll the query endpoint until the task reaches a terminal state.
            file_id = None
            while True:
                time.sleep(5)
                status_resp = requests.get(
                    f"{base_url}/v1/query/video_generation",
                    headers=headers,
                    params={"task_id": task_id},
                    timeout=15,
                )
                status_resp.raise_for_status()
                status_data = status_resp.json()
                base_err = self._base_resp_error(status_data)
                if base_err:
                    return ToolResult(success=False, error=base_err)
                status = status_data.get("status", "")
                if status == _TERMINAL_SUCCESS:
                    file_id = status_data.get("file_id")
                    break
                if status == _TERMINAL_FAIL:
                    return ToolResult(
                        success=False,
                        error="MiniMax video generation failed.",
                    )

            if not file_id:
                return ToolResult(
                    success=False,
                    error="MiniMax API did not return a file_id on success.",
                )

            # Retrieve the download URL for the generated file.
            file_resp = requests.get(
                f"{base_url}/v1/files/retrieve",
                headers=headers,
                params={"file_id": file_id},
                timeout=30,
            )
            file_resp.raise_for_status()
            file_data = file_resp.json()
            base_err = self._base_resp_error(file_data)
            if base_err:
                return ToolResult(success=False, error=base_err)
            download_url = (file_data.get("file") or {}).get("download_url")
            if not download_url:
                return ToolResult(
                    success=False,
                    error="MiniMax API did not return a download_url.",
                )

            video_response = requests.get(download_url, timeout=120)
            video_response.raise_for_status()

            output_path = Path(inputs.get("output_path", "minimax_output.mp4"))
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_bytes(video_response.content)

        except Exception as e:  # noqa: BLE001 - surface a redacted error to the caller
            return ToolResult(
                success=False,
                error=f"MiniMax video generation failed: {_redact(str(e))}",
            )

        return ToolResult(
            success=True,
            data={
                "provider": "minimax",
                "model": model,
                "region": base_url,
                "task_id": task_id,
                "file_id": file_id,
                "prompt": inputs.get("prompt", ""),
                "output": str(output_path),
            },
            artifacts=[str(output_path)],
            cost_usd=self.estimate_cost(inputs),
            duration_seconds=round(time.time() - start, 2),
            model=model,
        )
