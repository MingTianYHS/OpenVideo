"""Regression tests for explicit Agnes region routing."""

from lib.providers.agnes import AgnesClient


def test_explicit_cn_region_ignores_stale_legacy_global_url(monkeypatch):
    monkeypatch.setenv("AGNES_API_KEY", "legacy-key")
    monkeypatch.setenv("AGNES_BASE_URL", "https://apihub.agnes-ai.com")
    monkeypatch.setenv("AGNES_REGION", "cn")
    monkeypatch.delenv("AGNES_CN_BASE_URL", raising=False)

    client = AgnesClient()

    assert client.region == "cn"
    assert client.base_url == "https://api.agnes-ai.cn"


def test_per_call_global_region_ignores_stale_legacy_cn_url(monkeypatch):
    monkeypatch.setenv("AGNES_API_KEY", "legacy-key")
    monkeypatch.setenv("AGNES_BASE_URL", "https://api.agnes-ai.cn")
    monkeypatch.delenv("AGNES_REGION", raising=False)
    monkeypatch.delenv("AGNES_GLOBAL_BASE_URL", raising=False)

    client = AgnesClient(region="global")

    assert client.region == "global"
    assert client.base_url == "https://apihub.agnes-ai.com"
