"""插件发现、校验与加载。

安全边界（首版明确）：
- 只支持**可信本地插件**。清单声明权限 ≠ 自动获得授权，工具调用仍统一经过
  权限运行时；本加载器**不提供**不可信第三方代码沙箱。
- 单个插件加载失败只禁用该插件并记录 plugin_error，核心继续运行。
"""
from __future__ import annotations

import importlib.util
import sys
import uuid
from pathlib import Path
from typing import Any

import yaml

from .. import config as cfg_mod
from .sdk import Plugin, PluginError, ToolSpec


class LoadedPlugin:
    def __init__(self, manifest: dict, path: Path, instance: Plugin | None = None,
                 error: str | None = None, enabled: bool = True):
        self.manifest = manifest
        self.path = path
        self.instance = instance
        self.error = error
        self.enabled = enabled
        self.data_dir = str(path)

    @property
    def name(self) -> str:
        return self.manifest.get("name", self.path.name)

    @property
    def version(self) -> str:
        return self.manifest.get("version", "0.0.0")

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "version": self.version,
            "description": self.manifest.get("description", ""),
            "path": str(self.path),
            "permissions": self.manifest.get("permissions", []),
            "tools": self.manifest.get("tools", []),
            "enabled": self.enabled and self.error is None,
            "error": self.error,
        }


def _discover_dirs(cfg: dict) -> list[Path]:
    dirs = [cfg_mod.REPO_ROOT / "plugins"]
    scenario_plugins = cfg_mod.REPO_ROOT / "longflow" / "scenarios"
    if scenario_plugins.exists():
        dirs.append(scenario_plugins)
    return [d for d in dirs if d.exists()]


def _load_module(path: Path, mod_name: str):
    spec = importlib.util.spec_from_file_location(mod_name, path)
    if spec is None or spec.loader is None:
        raise PluginError(f"无法加载模块: {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = module
    spec.loader.exec_module(module)
    return module


def _find_plugin_class(module) -> type[Plugin] | None:
    for attr in vars(module).values():
        if isinstance(attr, type) and issubclass(attr, Plugin) and attr is not Plugin:
            return attr
    return None


def load_plugins(cfg: dict) -> list[LoadedPlugin]:
    """发现并加载所有启用插件。返回 LoadedPlugin 列表（含失败项）。"""
    loaded: list[LoadedPlugin] = []
    plugin_cfg = cfg.get("plugins", {}) or {}

    for base in _discover_dirs(cfg):
        for manifest_path in sorted(base.glob("*/plugin.yaml")):
            plugin_dir = manifest_path.parent
            try:
                manifest = yaml.safe_load(manifest_path.read_text(encoding="utf-8")) or {}
                name = manifest.get("name")
                if not name:
                    raise PluginError("plugin.yaml 缺少 name")
                enabled_cfg = plugin_cfg.get(name, {}) or {}
                if enabled_cfg.get("enabled") is False:
                    loaded.append(LoadedPlugin(manifest, plugin_dir, enabled=False))
                    continue
                entry = manifest.get("entry")
                if not entry:
                    # 自动发现：plugin.py 优先，其次 <name>_plugin.py / 任意 *_plugin.py
                    candidates = [plugin_dir / "plugin.py", plugin_dir / f"{name}_plugin.py"]
                    candidates += sorted(plugin_dir.glob("*_plugin.py"))
                    entry_path = next((c for c in candidates if c.exists()), None)
                    if entry_path is None:
                        raise PluginError("未找到插件入口（plugin.py 或 *_plugin.py）")
                else:
                    entry_path = plugin_dir / entry
                module = _load_module(entry_path, f"longflow_plugin_{name}_{uuid.uuid4().hex[:6]}")
                cls = _find_plugin_class(module)
                if cls is None:
                    raise PluginError(f"{entry} 中未找到 Plugin 子类")
                instance = cls()
                instance.manifest = manifest
                instance.setup(enabled_cfg.get("config", {}) or manifest.get("config", {}), str(plugin_dir))
                loaded.append(LoadedPlugin(manifest, plugin_dir, instance=instance))
            except Exception as exc:  # noqa: BLE001 - 插件失败隔离
                fallback_manifest = {"name": plugin_dir.name, "version": "?"}
                try:
                    fallback_manifest = yaml.safe_load(manifest_path.read_text(encoding="utf-8")) or fallback_manifest
                except Exception:
                    pass
                loaded.append(LoadedPlugin(fallback_manifest, plugin_dir, error=str(exc)[:300]))
    return loaded


def collect_tools(loaded: list[LoadedPlugin]) -> list[tuple[ToolSpec, LoadedPlugin]]:
    """从已加载插件收集工具 spec。"""
    out = []
    for lp in loaded:
        if lp.instance is None or lp.error:
            continue
        try:
            for t in lp.instance.get_tools() or []:
                if isinstance(t, ToolSpec):
                    out.append((t, lp))
        except Exception as exc:  # noqa: BLE001
            lp.error = f"get_tools 失败: {str(exc)[:200]}"
    return out


def collect_knowledge(loaded: list[LoadedPlugin]) -> list[tuple[list[dict], LoadedPlugin]]:
    out = []
    for lp in loaded:
        if lp.instance is None or lp.error:
            continue
        try:
            chunks = lp.instance.get_knowledge() or []
            if chunks:
                out.append((chunks, lp))
        except Exception:  # noqa: BLE001
            continue
    return out


def dispatch_event(loaded: list[LoadedPlugin], kind: str, detail: dict) -> None:
    for lp in loaded:
        if lp.instance is None or lp.error:
            continue
        try:
            lp.instance.on_event(kind, detail)
        except Exception:  # noqa: BLE001 - 事件钩子不得影响主流程
            continue
