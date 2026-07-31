import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import fal_client
import httpx

from tools.video.seedance_video import SeedanceVideo


class _DownloadResponse:
    content = b"mock-video"

    def raise_for_status(self) -> None:
        return None


class _Handle:
    def __init__(self, statuses, result=None, result_error=None):
        self.request_id = "fal-request-123"
        self._statuses = iter(statuses)
        self._result = result or {"video": {"url": "https://files.example/video.mp4"}, "seed": 41001}
        self._result_error = result_error
        self.status_calls = 0
        self.get_calls = 0

    def status(self, *, with_logs=False):
        self.status_calls += 1
        status = next(self._statuses)
        if isinstance(status, Exception):
            raise status
        return status

    def get(self):
        self.get_calls += 1
        if self._result_error:
            raise self._result_error
        return self._result


def _http_error(status_code=422, body=None):
    body = body or {"detail": [{"loc": ["body", "image_urls"], "msg": "invalid reference"}]}
    request = httpx.Request("GET", "https://queue.fal.run/model/requests/id")
    response = httpx.Response(
        status_code,
        content=json.dumps(body).encode(),
        headers={
            "x-fal-request-id": "fal-request-123",
            "x-request-id": "trace-456",
            "content-type": "application/json",
        },
        request=request,
    )
    return fal_client.FalClientHTTPError(
        message="unprocessable entity",
        status_code=status_code,
        response_headers=dict(response.headers),
        response=response,
        error_type="unprocessable_entity",
    )


def _inputs(output_path):
    return {
        "scene_id": "shot-01",
        "prompt": "@Image1 and @Image2 in one continuous shot",
        "preferred_provider": "seedance",
        "preferred_provider_gap": 1.0,
        "allowed_providers": ["seedance"],
        "operation": "reference_to_video",
        "model_variant": "standard",
        "duration": "4",
        "aspect_ratio": "16:9",
        "resolution": "720p",
        "generate_audio": True,
        "seed": 41001,
        "output_path": str(output_path),
        "reference_image_urls": ["https://files.example/a.jpeg", "https://files.example/b.png"],
    }


class SeedanceVideoDiagnosticTests(unittest.TestCase):
    def setUp(self):
        os.environ["FAL_KEY"] = "test-id:test-secret"

    def test_outbound_payload_contains_only_provider_fields(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "video.mp4"
            handle = _Handle([fal_client.Completed(logs=[], metrics={})])
            with (
                patch("fal_client.submit", return_value=handle) as submit,
                patch("requests.get", return_value=_DownloadResponse()),
                patch("tools.video._shared.probe_output", return_value={"duration_seconds": 4.0}),
            ):
                result = SeedanceVideo().execute(_inputs(output))

        self.assertTrue(result.success)
        submit.assert_called_once()
        application, payload = submit.call_args.args
        self.assertEqual(application, "bytedance/seedance-2.0/reference-to-video")
        self.assertEqual(
            set(payload),
            {"prompt", "duration", "aspect_ratio", "resolution", "generate_audio", "seed", "image_urls"},
        )
        self.assertEqual(payload["image_urls"], ["https://files.example/a.jpeg", "https://files.example/b.png"])
        self.assertNotIn("reference_image_urls", payload)
        for field in (
            "scene_id",
            "preferred_provider",
            "preferred_provider_gap",
            "allowed_providers",
            "operation",
            "model_variant",
            "output_path",
        ):
            self.assertNotIn(field, payload)

    def test_polling_422_preserves_complete_diagnostics(self):
        error = _http_error()
        handle = _Handle([fal_client.InProgress(logs=[]), error])
        with (
            tempfile.TemporaryDirectory() as directory,
            patch("fal_client.submit", return_value=handle) as submit,
            patch("time.sleep"),
        ):
            result = SeedanceVideo().execute(_inputs(Path(directory) / "video.mp4"))

        self.assertFalse(result.success)
        self.assertEqual(submit.call_count, 1)
        self.assertEqual(result.data["http_status"], 422)
        self.assertEqual(result.data["fal_request_id"], "fal-request-123")
        self.assertEqual(result.data["terminal_queue_status"], "INPROGRESS")
        self.assertEqual(
            json.loads(result.data["response_body"]),
            {"detail": [{"loc": ["body", "image_urls"], "msg": "invalid reference"}]},
        )
        self.assertEqual(result.data["response_headers"]["x-fal-request-id"], "fal-request-123")
        self.assertEqual(result.data["response_headers"]["x-request-id"], "trace-456")
        self.assertGreaterEqual(result.data["elapsed_processing_seconds"], 0)

    def test_result_422_preserves_response_body(self):
        error = _http_error(body={"detail": "terminal result validation failed"})
        handle = _Handle(
            [fal_client.Completed(logs=[], metrics={})],
            result_error=error,
        )
        with tempfile.TemporaryDirectory() as directory, patch("fal_client.submit", return_value=handle):
            result = SeedanceVideo().execute(_inputs(Path(directory) / "video.mp4"))

        self.assertFalse(result.success)
        self.assertEqual(result.data["phase"], "result")
        self.assertEqual(result.data["http_status"], 422)
        self.assertEqual(json.loads(result.data["response_body"]), {"detail": "terminal result validation failed"})

    def test_polling_never_submits_twice(self):
        handle = _Handle(
            [
                fal_client.Queued(position=1),
                fal_client.InProgress(logs=[]),
                fal_client.Completed(logs=[], metrics={}),
            ]
        )
        with (
            tempfile.TemporaryDirectory() as directory,
            patch("fal_client.submit", return_value=handle) as submit,
            patch("time.sleep"),
            patch("requests.get", return_value=_DownloadResponse()),
            patch("tools.video._shared.probe_output", return_value={"duration_seconds": 4.0}),
        ):
            result = SeedanceVideo().execute(_inputs(Path(directory) / "video.mp4"))

        self.assertTrue(result.success)
        self.assertEqual(submit.call_count, 1)
        self.assertEqual(handle.status_calls, 3)
        self.assertEqual(handle.get_calls, 1)


if __name__ == "__main__":
    unittest.main()
