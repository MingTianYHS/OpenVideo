"""Regression test: google_music must request the global Vertex location.

Lyria 3 is only served from the "global" Vertex location. When the genai
client was built with the default region (e.g. us-central1 from
GOOGLE_CLOUD_LOCATION), every Vertex-authenticated generation failed with
`400 Lyria 3 is only supported in the global location.` The tool must pass
an explicit location override so the region env var cannot break music
generation.
"""

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))


def test_google_music_builds_client_with_global_location(monkeypatch, tmp_path):
    monkeypatch.setenv("GOOGLE_API_KEY", "test-key")
    monkeypatch.setenv("GOOGLE_CLOUD_LOCATION", "us-central1")

    import tools.google_credentials as gc

    captured: dict = {}

    class _Boom(Exception):
        pass

    def _fake_get_genai_client(http_options=None, location=None):
        captured["location"] = location
        raise _Boom("stop before any network call")

    monkeypatch.setattr(gc, "get_genai_client", _fake_get_genai_client)

    from tools.audio.google_music import GoogleMusic

    result = GoogleMusic().execute(
        {"prompt": "solo piano", "output_path": str(tmp_path / "m.mp3")}
    )

    # The client factory must be asked for the global location regardless of
    # GOOGLE_CLOUD_LOCATION; the stub then aborts before any network call.
    assert captured["location"] == "global"
    assert not result.success
