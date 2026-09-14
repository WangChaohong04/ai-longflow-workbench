"""Mock transport checks; these do not count as real API verification."""
from types import SimpleNamespace
import httpx
import pytest
from longflow import llm, config
from longflow.search_provider import default_provider
from longflow.subagent_runner import SubagentRunner


@pytest.mark.parametrize("payload", [[], None, "not json", '{"ok":'])
def test_model_invalid_content_never_falls_back_in_strict(monkeypatch, payload):
    import json
    content = payload if isinstance(payload, str) else json.dumps(payload)
    monkeypatch.setattr(llm.httpx, "post", lambda *a, **k: httpx.Response(
        200, json={"choices": [{"message": {"content": content}}]},
        request=httpx.Request("POST", "https://model.example.com/v1/chat/completions")))
    driver = llm.OpenAICompatibleDriver("https://model.example.com/v1", "model", "test", strict=True, max_retries=0)
    with pytest.raises(llm.LLMError):
        driver._chat_json("json", "ping")
    assert all(not r["ok"] and not r["fallback"] for r in driver.drain_reports())


@pytest.mark.parametrize("override", [{"model": ""}, {"base_url": None}, {"api_key": ""}])
def test_incomplete_strict_configuration_is_rejected(override):
    settings = {"driver": "openai_compatible", "base_url": "https://model.example.com/v1",
                "model": "model", "api_key": "test", "strict": True, **override}
    with pytest.raises(llm.LLMError):
        llm.build_driver({"llm": settings})


def test_search_configuration_reaches_real_runner_class(monkeypatch):
    # Only DNS is deterministic here; MockTransport never contacts the Internet.
    monkeypatch.setattr("longflow.netguard.socket.getaddrinfo", lambda *a: [])
    monkeypatch.setenv("LONGFLOW_SEARCH_ENDPOINT", "https://search.example.com/search")
    monkeypatch.setenv("LONGFLOW_SEARCH_ALLOWED_DOMAINS", "example.com")
    cfg = config.load_config()
    seen = []
    def handler(req):
        seen.append(str(req.url))
        return httpx.Response(200, json={"results": [{"url": "https://docs.example.com/rule",
            "title": "规则", "snippet": "规则的有效摘要"}]})
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        runner = SubagentRunner(SimpleNamespace(http=client), cfg=cfg)
        records = runner.provider.search("规则")
    assert seen and runner.provider.name == "http"
    assert records[0].title == "规则" and records[0].collected_at


@pytest.mark.parametrize("status", [401, 429, 500, 302])
def test_search_http_errors_are_not_empty_success(monkeypatch, status):
    monkeypatch.setattr("longflow.netguard.socket.getaddrinfo", lambda *a: [])
    with httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(status))) as client:
        provider = default_provider(client, {"search_endpoint": "https://example.com/search",
                                            "allowed_domains": ["example.com"]})
        with pytest.raises(RuntimeError, match="search_http_status"):
            provider.search("rule")


def test_search_requires_admin_allowlist():
    assert default_provider(object(), {"search_endpoint": "https://example.com"}).name == "null"
