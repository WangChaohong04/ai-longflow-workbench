"""敏感信息检测与脱敏。

数据发送给模型前、结果返回前、写入日志前调用。密钥模式兜底，
即使配置失误也不进入日志。
"""
from __future__ import annotations

import re
from typing import Any

# (名称, 正则, 替换)
_PATTERNS = [
    ("api_key", re.compile(r"(?i)(sk-[A-Za-z0-9_\-]{8,})"), "[REDACTED_API_KEY]"),
    (
        "bearer",
        re.compile(r"(?i)(bearer\s+)[A-Za-z0-9_\-\.]{12,}"),
        r"\1[REDACTED_TOKEN]",
    ),
    (
        "long_token",
        re.compile(r"(?<![A-Za-z0-9])[A-Za-z0-9_\-]{32,}(?![A-Za-z0-9])"),
        "[REDACTED_TOKEN]",
    ),
    ("email", re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}"), "[REDACTED_EMAIL]"),
    # 中国大陆手机号
    ("phone", re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)"), "[REDACTED_PHONE]"),
    # 身份证号（18 位）
    ("idcard", re.compile(r"(?<!\d)\d{17}[\dXx](?!\d)"), "[REDACTED_ID]"),
    # 银行卡号（13-19 位连续数字，宽松）
    ("bankcard", re.compile(r"(?<!\d)\d{16,19}(?!\d)"), "[REDACTED_BANKCARD]"),
]

_SECRET_KEYS = re.compile(r"(?i)(api_?key|token|secret|password|passwd|authorization)")


def redact(text: str) -> str:
    if not isinstance(text, str):
        return text
    out = text
    for _name, pat, repl in _PATTERNS:
        out = pat.sub(repl, out)
    return out


def redact_obj(obj: Any) -> Any:
    """递归脱敏 dict/list/str；键名疑似密钥的值直接遮蔽。"""
    if isinstance(obj, str):
        return redact(obj)
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            if isinstance(k, str) and _SECRET_KEYS.search(k) and isinstance(v, str) and v:
                out[k] = "[REDACTED]"
            else:
                out[k] = redact_obj(v)
        return out
    if isinstance(obj, (list, tuple)):
        return [redact_obj(v) for v in obj]
    return obj
