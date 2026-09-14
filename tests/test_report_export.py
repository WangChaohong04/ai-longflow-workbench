"""问题6c：能力状态总览（脱敏、无密钥） + 调研导出（Markdown/CSV 保留引用/时间/未确认/失败分支）。"""
from __future__ import annotations

import json
from types import SimpleNamespace

from fastapi.testclient import TestClient

from longflow import api as api_mod
from longflow import config as cfg_mod, report as rep
from longflow.evidence import LAYER_FACT, LAYER_UNCONFIRMED
from tests.conftest import tmp_db


def _api(workdir):
    cfg = cfg_mod.load_config()
    cfg["db_path"] = str(workdir / "rep.db")
    return TestClient(api_mod.create_app(cfg))


def test_unit_export_markdown_and_csv():
    root = SimpleNamespace(
        objective="10万以内家用车怎么选", title="t", status="partially_completed",
        result={"verified": False, "status_bucket": "partial",
                "branches_ok": ["icev"], "branches_failed": ["bev"],
                "failures": [{"branch": "bev", "error": "source_not_configured"}]})
    ev_fact = {"entity": "A车", "field": "price", "value": 10000, "unit": "元",
               "source_type": "api", "source_title": "厂商", "source_url": "https://x",
               "id": "e_abc", "collected_at": "2025-01-01", "page": None,
               "evidence_text": "官方报价 1 万元", "conversion": "1万→10000", "layer": LAYER_FACT}
    ev_unc = dict(ev_fact, entity="B车", value=8800, layer=LAYER_UNCONFIRMED, id="e_xyz",
                  source_type="document", source_title="文档")
    rows = [json.loads(json.dumps(x)) for x in (ev_fact, ev_unc)]
    md = rep.export_markdown(root, rows, root.result["failures"])
    assert "失败分支" in md and "bev" in md
    assert "e_abc" in md and "https://x" in md          # 引用/证据ID
    assert "（未确认）" in md                            # 未确认项显式标记
    assert "1万→10000" in md                            # 转换记录
    csv = rep.export_csv(root, rows)
    assert csv.startswith("task,") and "confirmed" in csv
    assert ",yes," in csv and ",no," in csv              # 已确认/未确认 在 csv 中区分


def test_api_capabilities_overview_sanitized(workdir):
    c = _api(workdir)
    r = c.get("/api/capabilities")
    assert r.status_code == 200
    caps = r.json()["capabilities"]
    assert caps and all("key" in x and "enabled" in x and "ready" in x for x in caps)
    blob = json.dumps(caps)
    # 不得泄露密钥/端点值/令牌（只允许布尔与说明文本）
    for cap in caps:
        for k in cap:
            assert k.lower() not in ("api_key", "web_key", "security_code", "token",
                                     "password", "secret"), f"泄露键: {k}"
    assert "search_endpoint=" not in blob  # 端点值不外显（仅 ready 布尔）


def test_api_export_task(workdir):
    from longflow import api as api_mod
    cfg = cfg_mod.load_config()
    cfg["db_path"] = str(workdir / "rep.db")
    app = api_mod.create_app(cfg)
    c = TestClient(app)
    r = c.post("/api/tasks", json={"goal": "10万以内家用车"})
    assert r.status_code == 200
    rid = r.json()["task"]["id"]
    st = app.state.lf
    eng = st.engine()
    eng.user_message(rid, "全部比较")
    _drive(eng, rid)
    rd = c.get(f"/api/tasks/{rid}/export", params={"format": "md"})
    assert rd.status_code == 200
    text = rd.text
    assert "导出" in text and "状态：" in text
    assert ("失败分支" in text) or ("成功分支" in text) or ("证据数" in text)
    rc = c.get(f"/api/tasks/{rid}/export", params={"format": "csv"})
    assert rc.status_code == 200
    assert rc.text.startswith("\ufefftask,") or rc.text.startswith("task,")


def _drive(eng, rid, n=30):
    last = None
    for _ in range(n):
        last = eng.tick(rid)["status"]
        if last in ("completed", "partially_completed", "failed", "cancelled"):
            break
    return last