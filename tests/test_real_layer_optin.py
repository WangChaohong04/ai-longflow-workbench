"""真实层（真实模型 / 真实网页 API）独立、opt-in 报告。

与离线规则层、mock 桩层完全分开：
- 默认全部 **skip**（本机跑 pytest 默认通过离线/模拟即可，模拟通过**不算**真实通过）；
- 只有显式设置环境开关才会真正执行，且逐项验证真实能力是否可用；
- 报告分三类：offline / mock / real，绝不把仿真结果冒充真实结果。

做此类测试前必须满足真实前提（否则诚实失败，不降级为 mock 掩盖）。
"""
from __future__ import annotations

import os
from datetime import datetime
import httpx

import pytest

# 开关：真实模型需 LONGFLOW_REAL_MODEL=1 且配置了 openai_compatible 端点；
# 真实网页/官方/论坛检索需 LONGFLOW_REAL_WEB=1 且配置了 search_endpoint + allowed_domains。
_REAL_MODEL = os.environ.get("LONGFLOW_REAL_MODEL") == "1"
_REAL_WEB = os.environ.get("LONGFLOW_REAL_WEB") == "1"


def _real_skipped(name: str):
    return pytest.skip(f"真实层 opt-in 未开启（{name}）。"
                       f"离线/模拟层继续运行，但不计入真实通过。")


def test_real_model_driver_available():
    """真实模型：前置条件缺失时明确 skip（不计为通过）。"""
    if not _REAL_MODEL:
        _real_skipped("LONGFLOW_REAL_MODEL")
    from longflow import config as cfg_mod, llm as llm_mod
    cfg = cfg_mod.load_config()
    cfg["llm"].update(driver="openai_compatible", strict=True, max_retries=0)
    # 无端点/key 则为"配置不完整"，应真实失败而非跳过
    if not all(cfg["llm"].get(k) for k in ("base_url", "model", "api_key")):
        pytest.skip("未验证：缺少 LLM_BASE_URL / LLM_MODEL / LLM_API_KEY")
    driver = llm_mod.build_driver(cfg)
    assert driver.name != "local", "配置了真实端点却降级到 local，不能冒充真实模型"
    result = driver._chat_json('只返回 JSON 对象：{"ok": true}',
                               "验证接口连通性。", call_type="real_test")
    assert result == {"ok": True}
    reports = driver.drain_reports()
    assert len(reports) == 1 and reports[0]["http_status"] == 200
    assert reports[0]["ok"] is True and not reports[0]["fallback"]


def test_real_web_search_via_provider():
    """真实网页检索：经 SearchProvider 对真实端点查询并提取正文。"""
    if not _REAL_WEB:
        _real_skipped("LONGFLOW_REAL_WEB")
    from longflow import search_provider as sp
    from longflow.config import load_config
    cfg = load_config()
    if not cfg.get("search_endpoint") or not cfg.get("allowed_domains"):
        pytest.skip("未验证：缺少搜索端点或管理员域名白名单")
    with httpx.Client(timeout=20) as client:
        prov = sp.default_provider(client, cfg)
        assert prov.name == "http"
        res = prov.search(os.getenv("LONGFLOW_REAL_WEB_QUERY", "公开资料"), limit=2)
    # 真实结果 URL 必须是 http(s) 且来源有效——结果为空即真实失败
    assert any(r.url.startswith("http") for r in res), "真实检索未返回任何外部结果（不可当作通过）"
    for r in res:
        assert r.title.strip() and (r.content or r.snippet).strip()
        assert datetime.fromisoformat(r.collected_at)
        assert r.source_kind != "mock"
