"""问题7（补齐）：真实层验证。

1) 真实独立服务器（uvicorn over TCP）：非 TestClient，实际加载 / 与 /app.js，验证 SPA 入口。
   该层不依赖任何 mock —— 走真实 ASGI/socket。
2) 真实浏览器 DOM（playwright，缺依赖默认 skip）：载入页面 -> 类型目标 -> 点击提交 -> 校验 DOM。
   未安装 playwright 时明确 skip（"真实测试主动开启且缺配置默认 skip"），装上即可运行。
"""
from __future__ import annotations

import socket
import threading
import time

import pytest

from longflow import api as api_mod
from longflow import config as cfg_mod


def _start_server(workdir):
    import uvicorn
    cfg = cfg_mod.load_config()
    cfg["db_path"] = str(workdir / "realserver.db")
    app = api_mod.create_app(cfg)
    tmp = socket.socket()
    tmp.bind(("127.0.0.1", 0))
    port = tmp.getsockname()[1]
    tmp.close()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port,
                                           log_level="error", lifespan="on"))
    th = threading.Thread(target=server.run, daemon=True)
    th.start()
    for _ in range(100):
        if getattr(server, "started", False):
            break
        time.sleep(0.05)
    if not getattr(server, "started", False):
        raise RuntimeError("uvicorn 未启动")
    return server, f"http://127.0.0.1:{port}", th


def test_real_server_serves_spa(workdir):
    import httpx
    server, base, th = _start_server(workdir)
    try:
        health = httpx.get(base + "/api/health", timeout=10)
        assert health.status_code == 200 and health.json()["ok"] is True
        r = httpx.get(base + "/", timeout=10)
        assert r.status_code == 200
        assert 'id="app"' in r.text and 'src="app.js"' in r.text
        js = httpx.get(base + "/app.js", timeout=10)
        assert js.status_code == 200
        assert "application/javascript" in js.headers.get("content-type", "") or "text/javascript" in js.headers.get("content-type", "")
    finally:
        server.should_exit = True
        th.join(timeout=10)


def test_real_browser_load_type_click_verify_dom(workdir):
    pytest.importorskip("playwright")  # 真实浏览器层：缺依赖则默认 skip
    from playwright.sync_api import sync_playwright

    server, base, th = _start_server(workdir)
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch()
            page = browser.new_page()
            page.goto(base, wait_until="networkidle", timeout=20000)
            # 健康指示变为"已连接"
            page.wait_for_selector("#health-text")
            # 切到新建任务，输入目标并提交
            page.click("[data-view=new]")
            page.wait_for_selector("#f-goal")
            page.fill("#f-goal", "差旅住宿标准是多少")
            page.click('button[type="submit"]:has-text("提交任务")')
            # DOM 出现成功反馈（页面切到任务列表或出现 toast）
            page.wait_for_selector("#tasks-view, #toast-wrap", timeout=15000)
            toast = page.locator("#toast-wrap").first.inner_text() or ""
            assert ("已提交" in toast) or page.locator("#tasks-view").count() > 0
            browser.close()
    finally:
        server.should_exit = True
        th.join(timeout=10)
