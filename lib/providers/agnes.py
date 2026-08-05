"""Shared Agnes AI API client helpers.

Supports both Agnes China and global API regions while keeping authentication,
base URL handling, retries, and error parsing consistent across media tools.
"""

from __future__ import annotations

import os
import time
from typing import Any


AGNES_BASE_URLS = {
    "cn": "https://api.agnes-ai.cn",
    "global": "https://apihub.agnes-ai.com",
}
_REGION_ALIASES = {
    "cn": "cn",
    "china": "cn",
    "zh-cn": "cn",
    "global": "global",
    "international": "global",
    "intl": "global",
}


class AgnesAPIError(RuntimeError):
    """Raised when Agnes AI returns an unsuccessful API response."""

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


def normalize_agnes_region(region: str | None = None) -> str:
    """Normalize a configured Agnes region without silently switching regions."""
    raw = (region or os.environ.get("AGNES_REGION") or "global").strip().lower()
    normalized = _REGION_ALIASES.get(raw)
    if not normalized:
        choices = ", ".join(sorted(AGNES_BASE_URLS))
        raise AgnesAPIError(f"Unknown Agnes region {raw!r}; choose one of: {choices}")
    return normalized


def agnes_api_key_for_region(region: str) -> str:
    """Return the region-specific key, falling back to the legacy shared key."""
    env_name = "AGNES_CN_API_KEY" if region == "cn" else "AGNES_GLOBAL_API_KEY"
    return os.environ.get(env_name) or os.environ.get("AGNES_API_KEY", "")


def agnes_base_url_for_region(region: str) -> str:
    """Return the region-specific API host.

    Region-specific variables take precedence. AGNES_BASE_URL remains supported
    for backward compatibility. Values should be host roots without a trailing
    /v1 because provider paths already include /v1.
    """
    env_name = "AGNES_CN_BASE_URL" if region == "cn" else "AGNES_GLOBAL_BASE_URL"
    value = os.environ.get(env_name) or os.environ.get("AGNES_BASE_URL") or AGNES_BASE_URLS[region]
    value = value.rstrip("/")
    if value.endswith("/v1"):
        value = value[:-3].rstrip("/")
    return value


def configured_agnes_regions() -> list[str]:
    """List regions that have a usable region-specific or legacy API key."""
    return [region for region in AGNES_BASE_URLS if agnes_api_key_for_region(region)]


class AgnesClient:
    """Minimal JSON client for Agnes AI media APIs."""

    def __init__(
        self,
        api_key: str | None = None,
        base_url: str | None = None,
        *,
        region: str | None = None,
        max_retries: int = 2,
    ) -> None:
        self.region = normalize_agnes_region(region)
        self.api_key = api_key or agnes_api_key_for_region(self.region)
        self.base_url = (base_url or agnes_base_url_for_region(self.region)).rstrip("/")
        self.max_retries = max(0, max_retries)
        self.connect_timeout = float(os.environ.get("AGNES_CONNECT_TIMEOUT_SECONDS", "15"))

        if not self.api_key:
            env_name = "AGNES_CN_API_KEY" if self.region == "cn" else "AGNES_GLOBAL_API_KEY"
            raise AgnesAPIError(
                f"No Agnes API key configured for region {self.region!r}; set {env_name} "
                "or the backward-compatible AGNES_API_KEY"
            )

    @property
    def headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

    def post_json(
        self,
        path: str,
        payload: dict[str, Any],
        *,
        timeout: int | float = 180,
    ) -> dict[str, Any]:
        return self._request_json("POST", path, payload=payload, timeout=timeout)

    def get_json(
        self,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        timeout: int | float = 30,
    ) -> dict[str, Any]:
        return self._request_json("GET", path, params=params, timeout=timeout)

    def _request_json(
        self,
        method: str,
        path: str,
        *,
        payload: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
        timeout: int | float,
    ) -> dict[str, Any]:
        import requests

        url = f"{self.base_url}/{path.lstrip('/')}"
        retryable_statuses = {429, 500, 502, 503, 504}
        request_timeout = (self.connect_timeout, float(timeout))

        for attempt in range(self.max_retries + 1):
            try:
                response = requests.request(
                    method,
                    url,
                    headers=self.headers,
                    json=payload,
                    params=params,
                    timeout=request_timeout,
                )
            except requests.RequestException as exc:
                if attempt >= self.max_retries:
                    raise AgnesAPIError(
                        f"Agnes {self.region} API request failed at {self.base_url}: {exc}"
                    ) from exc
                time.sleep(2**attempt)
                continue

            if response.ok:
                try:
                    data = response.json()
                except ValueError as exc:
                    raise AgnesAPIError(
                        f"Agnes {self.region} API returned a non-JSON response",
                        status_code=response.status_code,
                    ) from exc
                if not isinstance(data, dict):
                    raise AgnesAPIError(
                        f"Agnes {self.region} API returned an unexpected response shape",
                        status_code=response.status_code,
                    )
                return data

            message = self._error_message(response, self.region)
            if response.status_code not in retryable_statuses or attempt >= self.max_retries:
                raise AgnesAPIError(message, status_code=response.status_code)

            retry_after = response.headers.get("Retry-After")
            try:
                delay = float(retry_after) if retry_after else float(2**attempt)
            except ValueError:
                delay = float(2**attempt)
            time.sleep(max(0.0, delay))

        raise AgnesAPIError(f"Agnes {self.region} API request failed after retries")

    @staticmethod
    def _error_message(response: Any, region: str) -> str:
        detail: Any = None
        try:
            payload = response.json()
            if isinstance(payload, dict):
                detail = payload.get("error") or payload.get("message") or payload.get("detail")
        except ValueError:
            detail = None

        if isinstance(detail, dict):
            detail = detail.get("message") or detail.get("detail") or str(detail)
        if not detail:
            detail = (getattr(response, "text", "") or "").strip()[:500]
        if not detail:
            detail = "unknown error"
        return f"Agnes {region} API error ({response.status_code}): {detail}"
