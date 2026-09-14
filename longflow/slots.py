"""通用类型化槽位与澄清（不绑定任何业务领域）。

支持类型：text / number / money / time / location / enum / bool / list / object。
槽位定义（声明式，来自领域包）示例：
  {name, type, prompt, required_for:[intent|*], enum:[...], min, max, default,
   unit, multi:bool, fields:[...](object 子字段), ask_once:bool}

处理规则：
- 保存用户已提供信息，不重复询问已确认内容；
- 只询问影响任务范围/工具选择/结论的关键条件；每轮最多 1~2 问；
- 可选槽位用**明确标注**的默认值；
- 信息冲突时请求用户确认（不静默取值）；
- "都可以/全部比较/不确定" 允许展开多分支（标记 branch_all）。
"""
from __future__ import annotations

import re
from typing import Any

# 支持的槽位类型
T_TEXT = "text"
T_NUMBER = "number"
T_MONEY = "money"
T_TIME = "time"
T_LOCATION = "location"
T_ENUM = "enum"
T_BOOL = "bool"
T_LIST = "list"
T_OBJECT = "object"
SLOT_TYPES = {T_TEXT, T_NUMBER, T_MONEY, T_TIME, T_LOCATION, T_ENUM, T_BOOL, T_LIST, T_OBJECT}

# 用户表示"不确定/都可以/全部比较"
_BRANCH_ALL = ["都可以", "都行", "全部", "所有", "都比较", "全部比较", "不确定", "随便", "每个都", "各个"]


class SlotError(ValueError):
    pass


def is_branch_all(text: str) -> bool:
    t = text or ""
    return any(h in t for h in _BRANCH_ALL)


def coerce(slot: dict, value: Any) -> Any:
    """按声明类型把用户输入归一化；类型非法抛 SlotError（不静默吞错）。"""
    typ = slot.get("type", T_TEXT)
    if typ not in SLOT_TYPES:
        raise SlotError(f"未知槽位类型: {typ}")

    if typ == T_TEXT:
        return str(value).strip()

    if typ in (T_NUMBER, T_MONEY):
        num = _to_number(value)
        if num is None:
            raise SlotError(f"{slot.get('name')} 需要数字，收到: {value!r}")
        if slot.get("min") is not None and num < float(slot["min"]):
            raise SlotError(f"{slot.get('name')} 不能小于 {slot['min']}")
        if slot.get("max") is not None and num > float(slot["max"]):
            raise SlotError(f"{slot.get('name')} 不能大于 {slot['max']}")
        return num

    if typ == T_BOOL:
        if isinstance(value, bool):
            return value
        s = str(value).strip().lower()
        if s in ("是", "对", "要", "需要", "yes", "y", "true", "1", "好"):
            return True
        if s in ("否", "不用", "不要", "no", "n", "false", "0", "不用了"):
            return False
        raise SlotError(f"{slot.get('name')} 需要是/否，收到: {value!r}")

    if typ == T_ENUM:
        allowed = slot.get("enum", [])
        s = str(value).strip()
        for opt in allowed:  # 允许枚举值是 {value,label}
            ov = opt.get("value") if isinstance(opt, dict) else opt
            if s == str(ov) or s in str(opt):
                return ov
        raise SlotError(f"{slot.get('name')} 必须是 {allowed} 之一，收到: {s!r}")

    if typ == T_LIST:
        if isinstance(value, list):
            items = value
        else:
            items = [x for x in re.split(r"[,，、;；\s]+", str(value)) if x]
        inner = dict(slot); inner["type"] = slot.get("item_type", T_TEXT)
        return [coerce(inner, x) for x in items]

    if typ == T_OBJECT:
        if not isinstance(value, dict):
            raise SlotError(f"{slot.get('name')} 需要结构化对象")
        out = dict(value)
        for f in slot.get("fields", []) or []:
            if f.get("name") in out and out[f["name"]] is not None:
                out[f["name"]] = coerce(f, out[f["name"]])
        return out

    # time / location：保留字符串（可扩展为日期/坐标解析）
    return str(value).strip()


def _to_number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    m = re.search(r"(\d+(?:\.\d+)?)", str(value))
    if not m:
        return None
    num = float(m.group(1))
    s = str(value)
    if "万" in s:
        num *= 10000
    elif "千" in s or re.search(r"\d\s*[kK]", s):
        num *= 1000
    return num


def validate_all(slot_defs: list[dict], values: dict) -> tuple[dict, list[dict]]:
    """对已给值做类型校验；返回 (归一化值, 错误列表[{name,error}])。"""
    cleaned, errors = {}, []
    by_name = {s["name"]: s for s in slot_defs}
    for name, val in (values or {}).items():
        if val is None or val == "":
            continue
        slot = by_name.get(name, {"name": name, "type": T_TEXT})
        try:
            cleaned[name] = coerce(slot, val)
        except SlotError as e:
            errors.append({"name": name, "error": str(e)})
    return cleaned, errors


def missing_slots(slot_defs: list[dict], values: dict, intent: str | None,
                  *, already_asked: set[str] | None = None, max_ask: int = 2) -> list[dict]:
    """返回本轮该问的缺失槽位（最多 max_ask 个），不重复问已确认/已问过的。"""
    already_asked = already_asked or set()
    out = []
    for slot in slot_defs:
        name = slot.get("name")
        if values.get(name) not in (None, "", []):
            continue
        required_for = slot.get("required_for", [])
        if not required_for:
            continue  # 可选槽位不主动追问
        if intent not in required_for and "*" not in required_for:
            continue
        if name in already_asked and not slot.get("re_ask"):
            continue
        out.append({"name": name, "prompt": slot.get("prompt", f"请提供 {name}"),
                    "type": slot.get("type", T_TEXT),
                    "enum": slot.get("enum"), "unit": slot.get("unit"),
                    "default": slot.get("default")})
        if len(out) >= max_ask:
            break
    return out


def apply_defaults(slot_defs: list[dict], values: dict) -> dict:
    """可选槽位用**明确标注**的默认值补齐（默认值在澄清时向用户明示）。"""
    out = dict(values or {})
    for slot in slot_defs:
        name = slot.get("name")
        if out.get(name) in (None, "", []) and slot.get("default") is not None:
            out[name] = slot["default"]
    return out


def detect_slot_conflicts(slot_defs: list[dict], current: dict, incoming: dict) -> list[dict]:
    """同一槽位新旧值不一致 -> 冲突，需用户确认（不静默覆盖）。"""
    conflicts = []
    for name, new_val in (incoming or {}).items():
        if new_val in (None, "", []):
            continue
        old_val = (current or {}).get(name)
        if old_val not in (None, "", []) and str(old_val) != str(new_val):
            conflicts.append({"name": name, "current": old_val, "incoming": new_val})
    return conflicts
