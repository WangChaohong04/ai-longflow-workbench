"""Batch4 追加：真实模型驱动的 usage 捕获、重试预算、strict 禁止降级、载荷脱敏。"""
import json

import httpx
import pytest

import longflow.llm as llm
from longflow.llm import OpenAICompatibleDriver, LLMError, LocalDriver


def _driver(strict=False, max_retries=1, handler=None):
    d = OpenAICompatibleDriver("http://x", "m", "k", strict=strict, max_retries=max_retries)
    if handler is not None:
        # monkeypatch httpx.post
        import longflow.llm as L
        L.httpx = handler
    return d


class FakeResp:
    def __init__(self, status=200, payload=None, raise_status=None):
        self.status_code = status
        self._payload = payload or {}
        self._raise = raise_status

    def raise_for_status(self):
        if self._raise:
            raise self._raise

    def json(self):
        return self._payload


def _ok_resp(content_obj, usage=None):
    return FakeResp(200, {
        "choices": [{"message": {"content": json.dumps(content_obj, ensure_ascii=False)}}],
        "usage": usage or {"prompt_tokens": 11, "completion_tokens": 7},
    })


def test_usage_captured_in_reports(monkeypatch):
    d = _driver()
    import longflow.llm as L
    monkeypatch.setattr(L.httpx, "post", lambda *a, **k: _ok_resp(
        {"type": "answer", "text": "ok"}, {"prompt_tokens": 21, "completion_tokens": 9}))
    class T: objective = "o"; agent_role = "executor"; slots = {}; plan = {}
    out = d.next_action(T(), {"tool_specs": [], "goal": "g"})
    assert out["type"] == "answer"
    reps = d.drain_reports()
    assert reps and reps[0]["ok"] is True
    assert reps[0]["prompt_tokens"] == 21 and reps[0]["completion_tokens"] == 9
    assert reps[0]["model"] == "m" and reps[0]["call_type"] == "next_action"


def test_failure_falls_back_and_records_report(monkeypatch):
    d = _driver(strict=False, max_retries=0)
    import longflow.llm as L
    def boom(*a, **k):
        raise httpx.ConnectError("unreachable")
    monkeypatch.setattr(L.httpx, "post", boom)
    class T: objective = "o"; agent_role = "executor"; slots = {}; plan = {}
    out = d.next_action(T(), {"tool_specs": [], "goal": "g"})
    # 降级到本地规则仍返回合法动作
    assert out.get("type") in ("tool_call", "answer")
    reps = d.drain_reports()
    fb = [r for r in reps if r.get("fallback")]
    assert fb, "应记录一条降级报告"
    assert all(r["ok"] is False for r in reps)


def test_strict_mode_raises_instead_of_silent_fallback(monkeypatch):
    d = _driver(strict=True, max_retries=0)
    import longflow.llm as L
    def boom(*a, **k):
        raise httpx.ConnectError("unreachable")
    monkeypatch.setattr(L.httpx, "post", boom)
    class T: objective = "o"; agent_role = "executor"; slots = {}; plan = {}
    with pytest.raises(LLMError) as ei:
        d.next_action(T(), {"tool_specs": [], "goal": "g"})
    assert "strict" in str(ei.value)


def test_retry_budget_caps_attempts(monkeypatch):
    d = _driver(strict=False, max_retries=2)
    import longflow.llm as L
    calls = {"n": 0}
    def boom(*a, **k):
        calls["n"] += 1
        raise httpx.ReadTimeout("slow")
    monkeypatch.setattr(L.httpx, "post", boom)
    class T: objective = "o"; agent_role = "executor"; slots = {}; plan = {}
    d.next_action(T(), {"tool_specs": [], "goal": "g"})  # 降级，不抛
    # max_retries=2 => 共 3 次尝试
    assert calls["n"] == 3


def test_401_not_retried(monkeypatch):
    d = _driver(strict=False, max_retries=3)
    import longflow.llm as L
    calls = {"n": 0}
    def resp(*a, **k):
        calls["n"] += 1
        return FakeResp(401, {})
    monkeypatch.setattr(L.httpx, "post", resp)
    class T: objective = "o"; agent_role = "executor"; slots = {}; plan = {}
    d.next_action(T(), {"tool_specs": [], "goal": "g"})
    assert calls["n"] == 1, "鉴权失败不应重试"


def test_payload_redacted_before_send(monkeypatch):
    d = _driver()
    import longflow.llm as L
    captured = {}
    def fake_post(url, headers=None, json=None, timeout=None):
        captured["body"] = json
        return _ok_resp({"type": "answer", "text": "ok"})
    monkeypatch.setattr(L.httpx, "post", fake_post)
    class T: objective = "o"; agent_role = "executor"; slots = {}; plan = {}
    ctx = {"tool_specs": [], "goal": "g",
           "evidence": {"api_key": "sk-1234567890abcdef1234567890abcdef"}}
    d.next_action(T(), ctx)
    blob = json.dumps(captured["body"], ensure_ascii=False)
    assert "sk-1234567890abcdef1234567890abcdef" not in blob, "发给模型的密钥应脱敏"
    assert "REDACTED" in blob


def test_build_driver_strict_missing_key_raises():
    with pytest.raises(LLMError):
        llm.build_driver({"llm": {"driver": "openai_compatible",
                                  "base_url": "http://x", "model": "m",
                                  "api_key": "", "strict": True}})


def test_build_driver_nonstrict_missing_key_falls_back():
    d = llm.build_driver({"llm": {"driver": "openai_compatible",
                                  "base_url": "http://x", "model": "m", "api_key": ""}})
    assert d.name == "local"
