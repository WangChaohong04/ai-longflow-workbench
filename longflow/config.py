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


def public_map_plugins(mp: dict) -> dict:
    """暴露给前端的地图插件配置（仅前端加载/渲染地图实际需要的字段）。

    - js_key：浏览器加载高德 JS SDK 的 Web 端 Key（公开值，应由高德后台限制 referer），需下发。
    - security_code：高德 JS API 2.0 securityJsCode，Demo 采用客户端方式，必须在加载 SDK 前
      设置 window._AMapSecurityConfig，故 Demo 下下发浏览器（不写死、不入日志）；生产可改 serviceHost。
    - web_key（AMAP_WEB_KEY）：服务端 Web Service Key，绝不下发浏览器。
    """
    def _amap(d: dict) -> dict:
        return {
            "enabled": bool(d.get("enabled")),
            "js_key": d.get("js_key", ""),
            "js_key_configured": bool(d.get("js_key")),
            # Demo：securityJsCode 客户端方式需要该值（生产建议 serviceHost 代理）
            "security_code": d.get("security_code", ""),
            "security_configured": bool(d.get("security_code")),
            "city": d.get("city", ""),
            # 注意：web_key（Web Service Key）刻意不包含，保持服务器端。
        }
    return {
        "active": mp.get("active", "amap"),
        "amap": _amap(mp.get("amap") or {}),
        "google": {"enabled": bool((mp.get("google") or {}).get("enabled")), "coming_soon": True},
    }
