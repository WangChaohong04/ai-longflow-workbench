"""配置加载：config/longflow.yaml + config/scenarios/*.yaml + 环境变量。

密钥只从环境变量读取，绝不写入配置文件、日志或仓库。
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = REPO_ROOT / "config" / "longflow.yaml"
SCENARIOS_DIR = REPO_ROOT / "config" / "scenarios"

_DEFAULTS: dict[str, Any] = {
    "llm": {"driver": "local", "base_url": "", "model": "local-default"},
    "plugins": {},
    "limits": {
        "max_steps": 30,
        "max_llm_calls": 80,
        "tick_interval_seconds": 2,
        "tool_timeout_seconds": 30,
        "max_verify_rounds": 2,
    },
    "server": {"host": "127.0.0.1", "port": 8765},
    "human_channel": None,  # 人工交接渠道；未配置时如实告知用户
    # 地图插件（前端工作台展示/路线/跳转用；Agent 的地理分析本身不依赖地图）。
    # active: 当前启用的地图插件；Key 只从环境变量读取，不入库不入仓库。
    "map_plugins": {
        "active": "amap",
        "amap": {"enabled": True, "city": "北京"},
        "google": {"enabled": False},  # 预留：未来切换
    },
}


def _deep_merge(base: dict, override: dict) -> dict:
    """深合并：结果中的嵌套 dict 均为新建副本，绝不与 base（如 _DEFAULTS）共享，
    避免对返回 cfg 的 setdefault/env 写入跨多次 load_config 累积造成配置污染。"""
    out = {}
    for k, v in base.items():
        out[k] = _deep_merge(v, {}) if isinstance(v, dict) else v
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = _deep_merge(v, {}) if isinstance(v, dict) else v
    return out


def load_config(path: Path | str | None = None) -> dict[str, Any]:
    """加载主配置；环境变量覆盖（LLM_*、LONGFLOW_DB）。"""
    path = Path(path) if path else CONFIG_PATH
    cfg = _deep_merge(_DEFAULTS, {})
    if path.exists():
        with open(path, "r", encoding="utf-8") as f:
            cfg = _deep_merge(cfg, yaml.safe_load(f) or {})

    # 环境变量覆盖
    if os.getenv("LONGFLOW_DRIVER"):
        cfg["llm"]["driver"] = os.environ["LONGFLOW_DRIVER"]
    if os.getenv("LLM_BASE_URL"):
        cfg["llm"]["base_url"] = os.environ["LLM_BASE_URL"]
    if os.getenv("LLM_MODEL"):
        cfg["llm"]["model"] = os.environ["LLM_MODEL"]
    cfg["llm"]["api_key"] = os.getenv("LLM_API_KEY", "")  # 只保留在内存
    cfg["db_path"] = os.getenv("LONGFLOW_DB") or str(REPO_ROOT / "data" / "longflow.db")

    # 地图插件 Key（前端工作台展示/路线/跳转用；Agent 地理分析不依赖地图）。
    # AMAP_JS_KEY 为高德 JS API key；AMAP_SECURITY_CODE 为安全密钥。
    # Key 只从环境变量读取，不写死、不入仓库、不入日志。
    mp = cfg.setdefault("map_plugins", {})
    amap = mp.setdefault("amap", {})
    # AMAP_JS_KEY：浏览器加载高德 JS SDK 所需的 Web 端 Key（由高德后台限制 referer）。
    # AMAP_WEB_KEY：服务端 Web Service Key，仅服务器使用，绝不下发浏览器、不兜底为 JS key。
    if os.getenv("AMAP_JS_KEY"):
        amap["js_key"] = os.environ["AMAP_JS_KEY"]
    if os.getenv("AMAP_WEB_KEY"):
        amap["web_key"] = os.environ["AMAP_WEB_KEY"]
    # AMAP_SECURITY_CODE：高德 JS API 2.0 的 securityJsCode。Demo 采用客户端方式，
    # 需要在浏览器加载 SDK 前设置 window._AMapSecurityConfig，因此下发到前端；
    # 不写死代码/仓库、不入日志。生产建议改用 serviceHost 代理（见 README）。
    if os.getenv("AMAP_SECURITY_CODE"):
        amap["security_code"] = os.environ["AMAP_SECURITY_CODE"]
    if os.getenv("AMAP_CITY"):
        amap["city"] = os.environ["AMAP_CITY"]
    return cfg


def list_scenarios() -> list[dict]:
    scenarios = []
    if not SCENARIOS_DIR.exists():
        return scenarios
    for p in sorted(SCENARIOS_DIR.glob("*.yaml")):
        with open(p, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        scenarios.append(
            {
                "name": data.get("name", p.stem),
                "description": data.get("description", ""),
                "file": p.name,
            }
        )
    return scenarios


def load_scenario(name: str) -> dict[str, Any]:
    path = SCENARIOS_DIR / f"{name}.yaml"
    if not path.exists():
        raise FileNotFoundError(f"场景配置不存在: {path}")
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def public_config(cfg: dict) -> dict:
    """返回可安全展示给前端/日志的配置（脱敏，绝不含密钥）。"""
    return {
        "llm": {
            "driver": cfg["llm"]["driver"],
            "model": cfg["llm"].get("model", ""),
            "base_url": cfg["llm"].get("base_url", ""),
            "api_key_configured": bool(cfg["llm"].get("api_key")),
        },
        "limits": cfg["limits"],
        "human_channel": cfg.get("human_channel"),
        "scenarios": list_scenarios(),
        "map_plugins": public_map_plugins(cfg.get("map_plugins") or {}),
    }


# 各地图插件可下发浏览器的字段白名单（其余字段——尤其服务端密钥——绝不公开）。
# 内置高德：JS SDK 前端加载需要 js_key 与（Demo securityJsCode 模式）security_code。
_PUBLIC_MAP_FIELDS: dict[str, set[str]] = {
    "amap": {"enabled", "js_key", "js_key_configured", "security_code", "security_configured", "city"},
    "google": {"enabled", "coming_soon"},
}


def public_map_plugins(mp: dict) -> dict:
    """暴露给前端的地图插件配置。

    - 内置插件按 _PUBLIC_MAP_FIELDS 白名单精确下发；
    - 第三方插件默认只下发 enabled；需额外字段时在 map_plugins.<id>.public_fields
      显式声明字段名（值取自该插件配置），避免把服务端密钥/原始配置整体公开；
    - AMAP_WEB_KEY 等服务端密钥不在任何白名单内，绝不下发。

    高德：js_key 为浏览器加载 SDK 的公开 Web Key（应在高德后台限制 Referer）；
    security_code 为 JS API 2.0 securityJsCode，Demo 客户端方式需在加载 SDK 前设置
    window._AMapSecurityConfig，故下发（不写死、不入日志）；生产可改 serviceHost 代理。
    """
    # 内置插件的字段默认值（保证前端拿到稳定结构）
    _DEFAULTS_OUT = {
        "amap": {"enabled": True, "js_key": "", "js_key_configured": False,
                 "security_code": "", "security_configured": False, "city": ""},
        "google": {"enabled": False, "coming_soon": True},
    }

    def _whitelisted(plugin_id: str, d: dict) -> dict:
        allowed = set(_PUBLIC_MAP_FIELDS.get(plugin_id, set()))
        # 第三方显式声明的可公开字段
        extra = d.get("public_fields")
        if isinstance(extra, (list, tuple)):
            allowed |= {str(x) for x in extra
                        if not str(x).startswith(("_", "web_"))
                        and not any(s in str(x).lower() for s in ("secret", "token", "api_key", "password"))}
        out = dict(_DEFAULTS_OUT.get(plugin_id, {"enabled": bool(d.get("enabled", plugin_id == "amap"))}))
        for k in sorted(allowed):
            if k in d:
                out[k] = d[k]
        # 派生标志
        if "js_key_configured" in allowed:
            out["js_key_configured"] = bool(d.get("js_key"))
        if "security_configured" in allowed:
            out["security_configured"] = bool(d.get("security_code"))
        return out

    out = {"active": mp.get("active", "amap")}
    for plugin_id, d in mp.items():
        if plugin_id in ("active", "public_fields") or not isinstance(d, dict):
            continue
        out[plugin_id] = _whitelisted(plugin_id, d)
    # google 占位（coming_soon 前端固定，不依赖配置）
    out.setdefault("google", {"enabled": False, "coming_soon": True})
    if "google" in out:
        out["google"]["coming_soon"] = True
    return out
