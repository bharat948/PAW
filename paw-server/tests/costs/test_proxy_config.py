import pytest

from paw_server.core.config import Settings

BASE = dict(database_url="postgresql://x:y@localhost/z", owner_token="t" * 20, env="ci")
TOKEN = "p" * 24


def settings(**kw):
    return Settings(_env_file=None, **{**BASE, **kw})  # type: ignore[arg-type]


def test_the_proxy_is_off_by_default_and_needs_no_provider_key():
    s = settings()
    assert s.model_proxy_token == "" and s.model_proxy_upstream_key == ""


def test_an_enabled_proxy_needs_a_long_distinct_token_and_a_provider_key():
    ok = settings(model_proxy_token=TOKEN, model_proxy_upstream_key="sk-x", gateway_token="g" * 24)
    assert ok.model_proxy_token == TOKEN
    for bad in (
        dict(model_proxy_token="short", model_proxy_upstream_key="sk-x"),
        dict(model_proxy_token=TOKEN, model_proxy_upstream_key=""),
        dict(model_proxy_token=TOKEN, model_proxy_upstream_key="sk-x", gateway_token=TOKEN),
    ):
        with pytest.raises(ValueError):
            settings(**bad)


def test_default_limits_match_the_approved_plan():
    s = settings()
    assert str(s.cost_shared_daily_usd) == "3" and s.model_proxy_default_output_tokens == 4096
