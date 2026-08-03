"""Shared Agnes AI API client helpers.

Keeps authentication, base URL handling, retries, and error parsing consistent
across the Agnes image and video provider tools.
"""

from __future__ import annotations

import os
import time
from typing import Any


DEFAULT_AGNES_BASE_URL = "https://apihub.agnes-ai.com"


class AgnesAPIError(RuntimeError):
    """Raised when Agnes AI returns an unsuccessful API response."""

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class AgnesClient:
    """Minimal JSON client for Agnes AI media APIs."""

    def __init__(
        self,
        api_key: str | None = None,
        base_url: str | None = None,
        *,
        max_retries: int = 2,
    ) -> None:
        self.api_key = api_key or os.environ.get("AGNES_API_KEY", "")
        self.base_url = (
            base_url or os.environ.get("AGNES_BASE_URL") or DEFAULT_AGNES_BASE_URL
        ).rstrip("/")
        self.max_retries = max(0, max_retries)

        if not self.api_key:
            raise AgnesAPIError("AGNES_API_KEY is not configured")

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
        timeout: int = 180,
    ) -> dict[str, Any]:
        return self._request_json("POST", path, payload=payload, timeout=timeout)

    def get_json(
        self,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        timeout: int = 30,
    ) -> dict[str, Any]:
        return self._request_json("GET", path, params=params, timeout=timeout)

    def _request_json(
        self,
        method: str,
        path: str,
        *,
        payload: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
        timeout: int,
    ) -> dict[str, Any]:
        import requests

        url = f"{self.base_url}/{path.lstrip('/')}"
        retryable_statuses = {429, 500, 502, 503, 504}

        for attempt in range(self.max_retries + 1):
            try:
                response = requests.request(
                    method,
                    url,
                    headers=self.headers,
                    json=payload,
                    params=params,
                    timeout=timeout,
                )
            except requests.RequestException as exc:
                if attempt >= self.max_retries:
                    raise AgnesAPIError(f"Agnes API request failed: {exc}") from exc
                time.sleep(2**attempt)
                continue

            if response.ok:
                try:
                    data = response.json()
                except ValueError as exc:
                    raise AgnesAPIError(
                        "Agnes API returned a non-JSON response",
                        status_code=response.status_code,
                    ) from exc
                if not isinstance(data, dict):
                    raise AgnesAPIError(
                        "Agnes API returned an unexpected response shape",
                        status_code=response.status_code,
                    )
                return data

            message = self._error_message(response)
            if response.status_code not in retryable_statuses or attempt >= self.max_retries:
                raise AgnesAPIError(message, status_code=response.status_code)

            retry_after = response.headers.get("Retry-After")
            try:
                delay = float(retry_after) if retry_after else float(2**attempt)
            except ValueError:
                delay = float(2**attempt)
            time.sleep(max(0.0, delay))

        raise AgnesAPIError("Agnes API request failed after retries")

    @staticmethod
    def _error_message(response: Any) -> str:
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
        return f"Agnes API error ({response.status_code}): {detail}"
