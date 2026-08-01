"""Focused tests for the ElevenLabs sound-effect generation tool.

No live API calls: the network layer is monkeypatched. Covers the tool
contract, registry discovery, status gating, payload construction, and
execute() guardrails.
"""

import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from tools.base_tool import BaseTool, ToolStatus, ToolTier, ToolRuntime
from tools.tool_registry import ToolRegistry
from tools.audio.sfx_gen import SfxGen
from tools.audio.music_gen import MusicGen


FAKE_MP3 = b"\xff\xfb\x90\x00" + b"\x00" * 32


class _FakeResponse:
    def __init__(self, content=FAKE_MP3, status_code=200, text=""):
        self.content = content
        self.status_code = status_code
        self.text = text


@pytest.fixture
def eleven_env(monkeypatch):
    monkeypatch.setenv("ELEVENLABS_API_KEY", "fake-key")


# ---- Contract ----

class TestContract:
    def test_inherits_base_tool(self):
        assert issubclass(SfxGen, BaseTool)

    def test_identity(self):
        t = SfxGen()
        assert t.name == "sfx_gen"
        assert t.capability == "sfx_generation"
        assert t.provider == "elevenlabs"
        assert t.runtime == ToolRuntime.API
        assert t.tier == ToolTier.GENERATE
        assert "sound-effects" in t.agent_skills
        assert "generate_sfx" in t.capabilities

    def test_music_gen_no_longer_claims_sfx(self):
        """generate_sfx moved here — music_gen must not double-claim it."""
        assert "generate_sfx" not in MusicGen.capabilities

    def test_declares_its_env_dependency(self):
        """Registry setup/dependency reporting groups providers by env var —
        a tool that needs a key but declares none cannot be explained."""
        assert SfxGen.dependencies == ["env:ELEVENLABS_API_KEY"]
        assert "ELEVENLABS_API_KEY" in SfxGen.install_instructions


# ---- Cost ----

class TestCost:
    """Billing is per minute of generated audio, so cost scales with duration
    rather than being one flat figure presented as durable."""

    def test_cost_scales_with_duration(self):
        t = SfxGen()
        short = t.estimate_cost({"prompt": "tick", "duration_seconds": 0.6})
        long = t.estimate_cost({"prompt": "bed", "duration_seconds": 20})
        assert short < long
        # $0.12/min list rate
        assert short == pytest.approx(0.6 / 60 * 0.12, abs=1e-4)
        assert long == pytest.approx(20 / 60 * 0.12, abs=1e-4)

    def test_auto_duration_uses_nominal_estimate(self):
        cost = SfxGen().estimate_cost({"prompt": "tick"})
        assert 0 < cost < 0.02

    def test_reported_cost_matches_estimate(self, eleven_env, tmp_path, monkeypatch):
        import requests

        monkeypatch.setattr(
            requests, "post",
            lambda *a, **k: _FakeResponse(),
        )
        inputs = {
            "prompt": "soft glass tick",
            "duration_seconds": 1.0,
            "output_path": str(tmp_path / "tick.mp3"),
        }
        res = SfxGen().execute(inputs)
        assert res.cost_usd == pytest.approx(SfxGen().estimate_cost(inputs))


# ---- Idempotency ----

class TestIdempotency:
    def test_omitted_defaults_hash_like_explicit_defaults(self):
        """Omitting prompt_influence/loop and passing their declared defaults
        describe the same request — they must not cause a duplicate paid call."""
        t = SfxGen()
        assert t.idempotency_key({"prompt": "tick"}) == t.idempotency_key(
            {"prompt": "tick", "prompt_influence": 0.3, "loop": False}
        )

    def test_real_differences_still_change_the_key(self):
        t = SfxGen()
        base = t.idempotency_key({"prompt": "tick"})
        assert base != t.idempotency_key({"prompt": "tick", "prompt_influence": 0.9})
        assert base != t.idempotency_key({"prompt": "tick", "loop": True})
        assert base != t.idempotency_key({"prompt": "whoosh"})


# ---- Registry discovery ----

class TestDiscovery:
    def test_discoverable(self):
        reg = ToolRegistry()
        reg.discover("tools")
        assert reg.get("sfx_gen") is not None

    def test_capability_routing(self):
        reg = ToolRegistry()
        reg.discover("tools")
        names = [t.name for t in reg.get_by_capability("sfx_generation")]
        assert names == ["sfx_gen"]

    def test_appears_in_provider_menu(self):
        reg = ToolRegistry()
        reg.discover("tools")
        menu = reg.provider_menu()
        assert "sfx_generation" in menu
        entries = menu["sfx_generation"]["available"] + menu["sfx_generation"]["unavailable"]
        entry = next(e for e in entries if e["name"] == "sfx_gen")
        assert entry["provider"] == "elevenlabs"
        assert "env:ELEVENLABS_API_KEY" in entry["dependencies"]

    def test_setup_offer_groups_by_env_var(self, monkeypatch):
        """Without the key the tool must show up as a 1-minute env-var fix,
        grouped with the other ELEVENLABS_API_KEY tools.

        Status is forced rather than unset via the environment: registry
        discovery reloads .env, so a developer machine with a real key would
        otherwise skip this assertion silently.
        """
        monkeypatch.setattr(SfxGen, "get_status", lambda self: ToolStatus.UNAVAILABLE)
        reg = ToolRegistry()
        reg.discover("tools")
        offers = reg.provider_menu_summary()["setup_offers"]
        offer = next((o for o in offers if o["tool"] == "sfx_gen"), None)
        assert offer is not None, "sfx_gen must be offered as a setup upgrade"
        assert offer["kind"] == "env_var"
        assert "ELEVENLABS_API_KEY" in offer["env_vars"]

    def test_capability_counted_in_summary(self, monkeypatch):
        monkeypatch.setattr(SfxGen, "get_status", lambda self: ToolStatus.AVAILABLE)
        reg = ToolRegistry()
        reg.discover("tools")
        caps = {c["capability"]: c for c in reg.provider_menu_summary()["capabilities"]}
        assert "sfx_generation" in caps
        assert "elevenlabs" in caps["sfx_generation"]["available_providers"]


# ---- Status ----

class TestStatus:
    def test_unavailable_without_key(self, monkeypatch):
        monkeypatch.delenv("ELEVENLABS_API_KEY", raising=False)
        assert SfxGen().get_status() == ToolStatus.UNAVAILABLE

    def test_available_with_key(self, eleven_env):
        assert SfxGen().get_status() == ToolStatus.AVAILABLE


# ---- execute() ----

class TestExecute:
    def test_missing_key(self, monkeypatch):
        monkeypatch.delenv("ELEVENLABS_API_KEY", raising=False)
        res = SfxGen().execute({"prompt": "soft glass tick"})
        assert not res.success
        assert "API key" in res.error

    def test_success_path_mocked(self, eleven_env, tmp_path, monkeypatch):
        import requests

        captured = {}

        def fake_post(url, headers=None, json=None, timeout=None):
            captured["url"] = url
            captured["headers"] = headers
            captured["payload"] = json
            return _FakeResponse()

        monkeypatch.setattr(requests, "post", fake_post)

        out = tmp_path / "sfx" / "tick.mp3"
        res = SfxGen().execute({
            "prompt": "single soft glass tick, short",
            "duration_seconds": 0.5,
            "prompt_influence": 0.6,
            "output_path": str(out),
        })

        assert res.success
        assert res.model == "elevenlabs-sound-generation"
        assert res.cost_usd == pytest.approx(0.5 / 60 * 0.12, abs=1e-4)
        assert out.read_bytes() == FAKE_MP3
        assert res.artifacts == [str(out)]
        assert captured["url"] == "https://api.elevenlabs.io/v1/sound-generation"
        assert captured["headers"]["xi-api-key"] == "fake-key"
        assert captured["payload"] == {
            "text": "single soft glass tick, short",
            "prompt_influence": 0.6,
            "duration_seconds": 0.5,
        }

    def test_loop_and_auto_duration(self, eleven_env, tmp_path, monkeypatch):
        import requests

        captured = {}
        monkeypatch.setattr(
            requests, "post",
            lambda url, headers=None, json=None, timeout=None: (
                captured.update(payload=json), _FakeResponse())[1],
        )
        res = SfxGen().execute({
            "prompt": "airy ambient hum",
            "loop": True,
            "output_path": str(tmp_path / "hum.mp3"),
        })
        assert res.success
        # no duration key when omitted (API auto-calculates); loop passed through
        assert "duration_seconds" not in captured["payload"]
        assert captured["payload"]["loop"] is True

    def test_http_error_surfaced(self, eleven_env, tmp_path, monkeypatch):
        import requests

        monkeypatch.setattr(
            requests, "post",
            lambda *a, **k: _FakeResponse(content=b"", status_code=422,
                                          text="duration out of range"),
        )
        res = SfxGen().execute(
            {"prompt": "tick", "output_path": str(tmp_path / "x.mp3")}
        )
        assert not res.success
        assert "422" in res.error
        assert "duration out of range" in res.error

    def test_request_exception_surfaced(self, eleven_env, tmp_path, monkeypatch):
        import requests

        def boom(*a, **k):
            raise requests.exceptions.ConnectionError("no route to host")

        monkeypatch.setattr(requests, "post", boom)
        res = SfxGen().execute(
            {"prompt": "tick", "output_path": str(tmp_path / "x.mp3")}
        )
        assert not res.success
        assert "no route to host" in res.error
