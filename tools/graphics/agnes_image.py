"""Agnes Image 2.1 generation, editing, and multi-image composition."""

from __future__ import annotations

import base64
import mimetypes
import os
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from lib.providers.agnes import AgnesAPIError, AgnesClient
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


_ALLOWED_RATIOS = {"1:1", "3:4", "4:3", "16:9", "9:16", "2:3", "3:2", "21:9"}
_ALLOWED_RESOLUTIONS = {"1K", "2K", "3K", "4K"}


def _file_to_data_uri(path_value: str) -> str:
    path = Path(path_value)
    if not path.is_file():
        raise FileNotFoundError(f"Input image not found: {path}")
    mime_type = mimetypes.guess_type(path.name)[0] or "image/png"
    encoded = base64.b64encode(path.read_bytes()).decode("ascii")
    return f"data:{mime_type};base64,{encoded}"


class AgnesImage(BaseTool):
    name = "agnes_image"
    version = "0.1.0"
    tier = ToolTier.GENERATE
    capability = "image_generation"
    provider = "agnes"
    stability = ToolStability.BETA
    execution_mode = ExecutionMode.SYNC
    determinism = Determinism.STOCHASTIC
    runtime = ToolRuntime.API

    dependencies = []
    install_instructions = (
        "Set AGNES_API_KEY to an Agnes AI API key.\n"
        "  Get one at https://platform.agnes-ai.com"
    )
    agent_skills = ["flux-best-practices"]

    capabilities = [
        "generate_image",
        "edit_image",
        "text_to_image",
        "image_to_image",
        "multi_image_composition",
    ]
    supports = {
        "image_edit": True,
        "multiple_reference_images": True,
        "aspect_ratio": True,
        "resolution": True,
        "base64_input": True,
        "base64_output": True,
    }
    best_for = [
        "high-information-density images and complex compositions",
        "composition-preserving image edits",
        "multi-image composition with URL or local references",
    ]
    not_good_for = ["offline generation", "strict seeded reproducibility"]
    fallback_tools = ["grok_image", "flux_image", "openai_image"]

    input_schema = {
        "type": "object",
        "required": ["prompt"],
        "properties": {
            "prompt": {"type": "string"},
            "generation_mode": {
                "type": "string",
                "enum": ["generate", "edit"],
                "default": "generate",
            },
            "model": {
                "type": "string",
                "enum": ["agnes-image-2.1-flash"],
                "default": "agnes-image-2.1-flash",
            },
            "resolution": {
                "type": "string",
                "enum": ["1K", "2K", "3K", "4K"],
                "default": "1K",
            },
            "aspect_ratio": {
                "type": "string",
                "enum": ["1:1", "3:4", "4:3", "16:9", "9:16", "2:3", "3:2", "21:9"],
                "default": "16:9",
            },
            "image_url": {"type": "string"},
            "image_path": {"type": "string"},
            "image_urls": {"type": "array", "items": {"type": "string"}},
            "image_paths": {"type": "array", "items": {"type": "string"}},
            "return_base64": {"type": "boolean", "default": False},
            "output_path": {"type": "string"},
        },
    }

    resource_profile = ResourceProfile(
        cpu_cores=1, ram_mb=512, vram_mb=0, disk_mb=200, network_required=True
    )
    retry_policy = RetryPolicy(
        max_retries=2,
        retryable_errors=["rate_limit", "timeout", "server_error"],
    )
    idempotency_key_fields = ["prompt", "model", "resolution", "aspect_ratio"]
    side_effects = ["writes image file(s) to output_path", "calls Agnes AI image API"]
    user_visible_verification = ["Inspect image quality, composition, and edit fidelity"]

    def get_status(self) -> ToolStatus:
        return ToolStatus.AVAILABLE if os.environ.get("AGNES_API_KEY") else ToolStatus.UNAVAILABLE

    def estimate_cost(self, inputs: dict[str, Any]) -> float:
        per_image = float(os.environ.get("AGNES_IMAGE_COST_PER_IMAGE", "0"))
        return per_image

    @staticmethod
    def _normalize_resolution(value: Any) -> str:
        normalized = str(value or "1K").upper()
        if normalized not in _ALLOWED_RESOLUTIONS:
            return "1K"
        return normalized

    @staticmethod
    def _normalize_ratio(value: Any) -> str:
        normalized = str(value or "16:9")
        if normalized not in _ALLOWED_RATIOS:
            return "16:9"
        return normalized

    @staticmethod
    def _reference_images(inputs: dict[str, Any]) -> list[str]:
        images: list[str] = []
        if inputs.get("image_url"):
            images.append(str(inputs["image_url"]))
        if inputs.get("image_path"):
            images.append(_file_to_data_uri(str(inputs["image_path"])))
        images.extend(str(url) for url in (inputs.get("image_urls") or []))
        images.extend(_file_to_data_uri(str(path)) for path in (inputs.get("image_paths") or []))
        return images

    def _build_payload(self, inputs: dict[str, Any]) -> dict[str, Any]:
        images = self._reference_images(inputs)
        mode = inputs.get("generation_mode", "generate")
        if mode == "edit" and not images:
            raise ValueError("Agnes image edit mode requires an input image")

        response_format = "b64_json" if inputs.get("return_base64") else "url"
        extra_body: dict[str, Any] = {"response_format": response_format}
        if images:
            extra_body["image"] = images

        return {
            "model": "agnes-image-2.1-flash",
            "prompt": inputs["prompt"],
            "size": self._normalize_resolution(inputs.get("resolution")),
            "ratio": self._normalize_ratio(inputs.get("aspect_ratio")),
            "extra_body": extra_body,
        }

    @staticmethod
    def _extension_from_url(url: str | None) -> str:
        suffix = Path(urlparse(url or "").path).suffix.lower()
        return suffix if suffix in {".png", ".jpg", ".jpeg", ".webp"} else ".png"

    @staticmethod
    def _output_paths(output_path: str | None, count: int, extension: str) -> list[Path]:
        if not output_path:
            return [Path(f"agnes_image_{index + 1}{extension}") for index in range(count)]
        path = Path(output_path)
        if count == 1:
            return [path if path.suffix else path.with_suffix(extension)]
        base = path.with_suffix("") if path.suffix else path
        suffix = path.suffix or extension
        return [base.parent / f"{base.name}_{index + 1}{suffix}" for index in range(count)]

    def execute(self, inputs: dict[str, Any]) -> ToolResult:
        if not os.environ.get("AGNES_API_KEY"):
            return ToolResult(success=False, error="AGNES_API_KEY not set. " + self.install_instructions)

        import requests

        start = time.time()
        try:
            payload = self._build_payload(inputs)
            response = AgnesClient().post_json("/v1/images/generations", payload, timeout=360)
            items = response.get("data") or []
            if not isinstance(items, list) or not items:
                return ToolResult(success=False, error="Agnes image response contained no outputs")

            extension = self._extension_from_url(items[0].get("url") if isinstance(items[0], dict) else None)
            output_paths = self._output_paths(inputs.get("output_path"), len(items), extension)
            outputs: list[str] = []

            for item, output_path in zip(items, output_paths):
                if not isinstance(item, dict):
                    raise AgnesAPIError("Agnes image response contained an invalid output item")
                output_path.parent.mkdir(parents=True, exist_ok=True)
                if item.get("b64_json"):
                    output_path.write_bytes(base64.b64decode(item["b64_json"]))
                elif item.get("url"):
                    download = requests.get(item["url"], timeout=180)
                    download.raise_for_status()
                    output_path.write_bytes(download.content)
                else:
                    raise AgnesAPIError("Agnes image output is missing url and b64_json")
                outputs.append(str(output_path))

        except Exception as exc:
            return ToolResult(success=False, error=f"Agnes image generation failed: {exc}")

        references = self._reference_images(inputs)
        mode = "edit" if references else "generate"
        return ToolResult(
            success=True,
            data={
                "provider": "agnes",
                "model": "agnes-image-2.1-flash",
                "prompt": inputs["prompt"],
                "generation_mode": mode,
                "output": outputs[0],
                "outputs": outputs,
                "images_generated": len(outputs),
                "resolution": payload["size"],
                "aspect_ratio": payload["ratio"],
            },
            artifacts=outputs,
            cost_usd=self.estimate_cost(inputs),
            duration_seconds=round(time.time() - start, 2),
            model="agnes-image-2.1-flash",
        )
