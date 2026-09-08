"""地图插件配置：AMAP_* 环境变量 → config → public_config 脱敏/下发规则。"""
import os

import pytest

from longflow.config import load_config, public_config, public_map_plugins


@pytest.fixture
def amap_env(monkeypatch):
    for k in ("AMAP_JS_KEY", "AMAP_WEB_KEY", "AMAP_SECURITY_CODE", "AMAP_CITY"):
        monkeypatch.delenv(k, raising=False)
    yield monkeypatch


def test_web_key_never_sent_to_browser(amap_env):
    """T3: AMAP_WEB_KEY 是服务端 Web Service Key，绝不出现在 public config。"""
    amap_env.setenv("AMAP_WEB_KEY", "WEB_SECRET_999")
    amap_env.setenv("AMAP_JS_KEY", "JSKEY123")
    cfg = load_config()
    pub = public_config(cfg)["map_plugins"]["amap"]
    assert "web_key" not in pub, "web_key 不得下发浏览器"
    assert "WEB_SECRET_999" not in str(pub), "Web Service Key 值不得出现在前端配置"
    # 服务端内部仍持有 web_key（供未来后端调用 Web Service）
    assert cfg["map_plugins"]["amap"].get("web_key") == "WEB_SECRET_999"


def test_public_config_only_browser_fields(amap_env):
    """T4: public config 只含前端实际需要的高德字段。"""
    amap_env.setenv("AMAP_JS_KEY", "JSKEY123")
    amap_env.setenv("AMAP_SECURITY_CODE", "SEC_456")
    amap_env.setenv("AMAP_CITY", "上海")
    pub = public_map_plugins(load_config()["map_plugins"])
    amap = pub["amap"]
    assert set(amap.keys()) <= {
        "enabled", "js_key", "js_key_configured",
        "security_code", "security_configured", "city",
    }
    assert amap["js_key"] == "JSKEY123"
    assert amap["js_key_configured"] is True
    assert amap["city"] == "上海"


def test_security_code_delivered_for_demo_securityjscode(amap_env):
    """Demo 采用 securityJsCode 客户端方式：security_code 需下发浏览器，
    以便在加载高德 JS SDK 前设置 window._AMapSecurityConfig。"""
    amap_env.setenv("AMAP_JS_KEY", "JSKEY123")
    amap_env.setenv("AMAP_SECURITY_CODE", "SEC_456")
    amap = public_config(load_config())["map_plugins"]["amap"]
    assert amap["security_code"] == "SEC_456"
    assert amap["security_configured"] is True


def test_no_security_code_when_unset(amap_env):
    amap_env.setenv("AMAP_JS_KEY", "JSKEY123")
    amap = public_config(load_config())["map_plugins"]["amap"]
    assert amap["security_code"] == ""
    assert amap["security_configured"] is False


def test_secrets_not_in_repr_loggable(monkeypatch):
    """T5: 配置加载/展示路径不主动打印密钥（public_config 输出不含 web key）。"""
    monkeypatch.setenv("AMAP_WEB_KEY", "WEB_SECRET_999")
    monkeypatch.setenv("AMAP_SECURITY_CODE", "SEC_456")
    pub = public_config(load_config())
    printed = str(pub)
    assert "WEB_SECRET_999" not in printed
