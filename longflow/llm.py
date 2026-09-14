"""LLM 网关：可替换模型路由。

- LocalDriver（默认）：确定性规则驱动，与真实模型产出同构的结构化动作，
  使编排/检索/工具/审批/恢复闭环在无 API Key 时也能真实跑通。
- OpenAICompatibleDriver：httpx 调用 OpenAI 兼容 /chat/completions。

模型只负责"提出下一步动作"，权限、调度、核验由 Harness 执行。
驱动返回的动作必须附带可核验信号；不允许只返回自报置信度。
"""
from __future__ import annotations

import json
import re
from typing import Any

import time as _time

import httpx

from . import redaction as _redact

ACTION_TOOL_CALL = "tool_call"
ACTION_ANSWER = "answer"
ACTION_CLARIFY = "clarify"
ACTION_HANDOFF = "handoff"
ACTION_RETRY = "retry"


class LLMError(Exception):
    pass


class BaseDriver:
    name = "base"

    @staticmethod
    def extract_slots(goal: str, slot_defs: list[dict]) -> dict:
        """Shared deterministic extraction, including API-backed drivers."""
        return LocalDriver.extract_slots(goal, slot_defs)

    def plan(self, goal: str, scenario_cfg: dict, slots: dict) -> dict:
        """返回 {intent, missing_slots:[{name,prompt}], handoff:bool, tasks:[{title,role,objective,tool,risk,depends:[idx]}]}"""
        raise NotImplementedError

    def next_action(self, task, ctx: dict) -> dict:
        """返回 {type, tool?, args?, reason?}。"""
        raise NotImplementedError

    def draft_answer(self, question: str, chunks: list[dict], slots: dict) -> dict:
        """返回 {text, citations:[chunk_id], claims:[...]}。"""
        raise NotImplementedError


# ---------------- LocalDriver ----------------

_INTENT_HANDOFF_HINTS = ["人工", "真人", "客服", "转人", "情绪", "投诉到底"]


class LocalDriver(BaseDriver):
    name = "local"

    # ---- 入口理解 ----
    # 明确动作信号：出现这些词才视为真的要执行动作，而非询问规则
    _ACTION_SIGNALS = ["我要", "帮我", "请", "申请", "提交", "下单", "采购 ", "买"]
    _QUERY_SIGNALS = ["规则", "政策", "制度", "标准", "流程", "是什么", "怎么", "如何",
                      "吗？", "吗?", "？", "?"]

    def detect_intent(self, goal: str, scenario_cfg: dict) -> str | None:
        keywords = scenario_cfg.get("intent_keywords", {}) or {}
        scores: dict[str, int] = {}
        for intent, words in keywords.items():
            scores[intent] = sum(1 for w in words if w in goal)
        best = max(scores, key=scores.get) if scores else None
        if not best or scores[best] == 0:
            return None
        # 消歧：含明确动作祈使才算动作意图；询问规则/流程/标准优先归到查询类
        is_query = any(s in goal for s in self._QUERY_SIGNALS)
        is_action = any(s in goal for s in self._ACTION_SIGNALS)
        action_intents = {"采购", "报销", "通知"}
        query_like = [i for i in scores if scores[i] > 0 and i not in action_intents]
        if best in action_intents and is_query and not is_action and query_like:
            return query_like[0]
        return best

    def plan(self, goal: str, scenario_cfg: dict, slots: dict) -> dict:
        intent = self.detect_intent(goal, scenario_cfg)
        # 含分类/场地线索（找+类别词，如“找咖啡馆”）优先归到“选址”，以使用带 filters 的模板
        if intent in ("附近", "场地") and re.search(r"找|推荐", goal):
            cat = re.search(r"(咖啡馆|咖啡|共享办公|图书馆|场地|会议室|门店|工位)", goal)
            if cat or slots.get("category"):
                intent = "选址"
        result: dict[str, Any] = {
            "intent": intent,
            "missing_slots": [],
            "handoff_requested": any(h in goal for h in _INTENT_HANDOFF_HINTS),
            "tasks": [],
            "signals": {"intent_keyword_matched": intent is not None},
        }
        if intent is None:
            return result

        # 必填槽位检查（影响下一步正确性才追问）
        for slot in scenario_cfg.get("slots", []) or []:
            required_for = slot.get("required_for", [])
            if intent in required_for and not slots.get(slot["name"]):
                result["missing_slots"].append(
                    {"name": slot["name"], "prompt": slot.get("prompt", f"请提供 {slot['name']}")}
                )

        templates = scenario_cfg.get("subtask_templates", {}) or {}
        tpl = templates.get(intent)
        if tpl:
            result["tasks"] = tpl
        return result

    # ---- 槽位抽取（用户自然语言中的数字/物品等） ----
    @staticmethod
    def extract_slots(goal: str, slot_defs: list[dict]) -> dict:
        found = {}
        # 1) 半径/距离优先抽取（数字紧邻 公里/km/千米/米）
        m = re.search(r"(\d+(?:\.\d+)?)\s*(公里|km|千米)", goal)
        if m:
            found["radius_km"] = float(m.group(1))
        # 2) 预算抽取（必须带货币单位 元/块/k/千/万；纯数字不猜预算，避免吞掉半径）
        for mm in re.finditer(r"(\d+(?:\.\d+)?)\s*(元|块|k|K|千|万)", goal):
            num = float(mm.group(1))
            unit = mm.group(2)
            # 紧邻“米”（如 400 米）是距离
            tail = goal[mm.end():mm.end() + 1]
            if unit in ("k", "K") and tail == "米":
                found.setdefault("radius_km", num / 1000.0)
                continue
            if unit in ("k", "K", "千"):
                found["budget"] = num * 1000
            elif unit == "万":
                found["budget"] = num * 10000
            else:
                found["budget"] = num
            break
        for slot in slot_defs:
            name = slot.get("name", "")
            if name in ("budget", "radius_km"):
                continue
            # 物品名：「采购X」「买X」「找X」
            m = re.search(r"(?:采购|购买|买|下单)\s*([一-龥A-Za-z0-9]{2,12})", goal)
            if m and name == "item":
                cand = m.group(1)
                # “东西/物品/什么”等泛指不算具体物品，槽位保持缺失 → 触发澄清
                if not re.match(r"^(东西|物品|什么|啥|商品|物资|设备的|一些|点)", cand):
                    found[name] = cand
            m = re.search(r"找(?:一个|个)?(?:安静的)?\s*([一-龥A-Za-z0-9]{2,8}?)(?:[，,。\s]|$)", goal)
            if m and name == "category" and not found.get("category"):
                found[name] = m.group(1)
            m = re.search(r"(?:在|去|到)([一-龥A-Za-z0-9]{2,12})(?:附近|周边|一带)", goal)
            if m and name == "location":
                found[name] = m.group(1)
            # “<地点>附近/周边” 无介词形式
            m = re.search(r"([一-龥A-Za-z0-9]{2,12})(?:附近|周边|一带)", goal)
            if m and name == "location" and not found.get("location"):
                found[name] = m.group(1)
        return found

    # ---- 下一步动作 ----
    def next_action(self, task, ctx: dict) -> dict:
        role = task.agent_role
        slots = task.slots
        evidence = ctx.get("evidence", {})

        if role == "researcher":
            tool = task.plan.get("tool_hint") or ctx.get("tool_hint")
            if tool and tool.startswith("geo_"):
                args = {
                    "center": slots.get("location") or slots.get("item"),
                    "radius_km": float(slots.get("radius_km") or 2),
                    "filters": {"category": slots["category"]} if slots.get("category") else None,
                    "sort_by": "distance",
                }
                args = {k: v for k, v in args.items() if v is not None}
                return {"type": ACTION_TOOL_CALL, "tool": tool, "args": args,
                        "reason": "空间检索候选地点"}
            return {
                "type": ACTION_TOOL_CALL,
                "tool": "kb_search",
                "args": {"query": task.objective + " " + ctx.get("goal", ""), "top_k": 8},
                "reason": "检索知识库获取依据",
            }

        if role == "executor":
            tool = task.plan.get("tool_hint") or ctx.get("tool_hint")
            if tool == "make_purchase":
                amount = float(slots.get("amount") or slots.get("budget") or 0)
                args = {
                    "vendor": slots.get("vendor", "approved-vendor"),
                    "item": slots.get("item", "未指定物品"),
                    "amount": amount,
                    "currency": "CNY",
                }
                return {"type": ACTION_TOOL_CALL, "tool": tool, "args": args,
                        "reason": "在核验通过的规则与预算内下单"}
            if tool == "send_notification":
                return {
                    "type": ACTION_TOOL_CALL,
                    "tool": tool,
                    "args": {
                        "channel": slots.get("channel", "ops"),
                        "to": slots.get("to", "行政"),
                        "text": f"采购申请已提交：{slots.get('item', '')}，预算 {slots.get('budget', '?')} 元",
                    },
                    "reason": "预授权渠道通知",
                }
            if tool and tool.startswith("geo_"):
                return {"type": ACTION_TOOL_CALL, "tool": tool,
                        "args": {"center": slots.get("location"), "radius_km": float(slots.get("radius_km") or 2)},
                        "reason": "空间计算"}
            return {"type": ACTION_ANSWER, "reason": "无待执行动作"}

        if role == "verifier":
            return {"type": ACTION_ANSWER, "reason": "汇总证据并交付核验结果"}

        return {"type": ACTION_ANSWER, "reason": "默认回答"}

    # ---- 起草答案（带引用占位） ----
    def draft_answer(self, question: str, chunks: list[dict], slots: dict) -> dict:
        if not chunks:
            return {
                "text": "未在已授权知识库中找到可支撑该问题的依据，无法确认。建议补充内部资料或联系相关负责人。",
                "citations": [],
                "no_evidence": True,
            }
        # 答案重排：优先引用含条款标识（第X条）、金额/规则动词且与问题词命中多的 chunk；
        # 纯版本/适用范围样板片段降权。
        q_terms = set(t for t in re.findall(r"[一-鿿]{2,}|[A-Za-z]{2,}", question))
        rule_kw = re.compile(r"第[一二三四五六七八九十百0-9]+条|审批|报销|限额|标准|规则|不得|必须")

        def rank(ch):
            text = ch.get("text", "")
            cite_bonus = 2.0 if ch.get("citations") else 0.0
            rule_hits = len(rule_kw.findall(text))
            amounts = len(re.findall(r"\d+(?:\.\d+)?\s*元", text))
            boilerplate = 1.0 if re.search(r"版本|生效日期|适用范围|文件：", text) and not rule_hits else 0.0
            qhit = sum(1 for w in q_terms if w in text)
            return cite_bonus + 0.8 * rule_hits + 1.2 * amounts + 0.5 * qhit - 3.0 * boilerplate

        ranked = sorted(chunks, key=rank, reverse=True)
        top = ranked[:3]
        lines = []
        citations = []
        for i, ch in enumerate(top, 1):
            cite_tags = "、".join(ch.get("citations") or []) or ch.get("section", "")
            snippet = ch["text"].strip().replace("\n", " ")
            if len(snippet) > 180:
                snippet = snippet[:180] + "…"
            lines.append(f"{i}. 根据{ch['doc_name']}{('（' + cite_tags + '）') if cite_tags else ''}：{snippet} [cite:{ch['chunk_id']}]")
            citations.append(ch["chunk_id"])
        text = "经查证授权知识库：\n" + "\n".join(lines)
        return {"text": text, "citations": citations, "no_evidence": False}


# ---------------- OpenAI 兼容驱动 ----------------

class OpenAICompatibleDriver(BaseDriver):
    name = "openai_compatible"

    def __init__(self, base_url: str, model: str, api_key: str, timeout: float = 60.0,
                 strict: bool = False, max_retries: int = 1):
        from urllib.parse import urlsplit
        endpoint = urlsplit(base_url or "")
        if endpoint.scheme not in ("http", "https") or not endpoint.hostname:
            raise LLMError("LLM_BASE_URL 必须是有效的 http(s) API 地址")
        if endpoint.username or endpoint.password or endpoint.query or endpoint.fragment:
            raise LLMError("LLM_BASE_URL 不允许包含凭据、查询参数或片段")
        if not isinstance(model, str) or not model.strip():
            raise LLMError("LLM_MODEL 未配置")
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.api_key = api_key
        self.timeout = timeout
        self.strict = strict              # True 时禁止自动降级到本地规则
        self.max_retries = max(0, int(max_retries))
        self._fallback = LocalDriver()
        self.reports: list[dict] = []     # 每次真实 HTTP 调用的可观察记录（由编排层抽取）
        if not api_key:
            raise LLMError("LLM_API_KEY 未配置")

    def drain_reports(self) -> list[dict]:
        out = self.reports
        self.reports = []
        return out

    @staticmethod
    def _redact_payload(obj):
        """发送给模型前对载荷脱敏（不破坏结构，仅遮蔽 key/secret/token/手机号/邮箱等）。"""
        return _redact.redact_obj(obj)

    def _chat_json(self, system: str, user: str, call_type: str = "chat") -> dict:
        # 预算化重试：仅对可恢复错误（网络/超时/限流/5xx/JSON 解析失败）重试；
        # 鉴权(401/403)与缺 key 不重试。每次尝试都落一条调用记录。
        payload = self._redact_payload({
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": 0.2,
            "response_format": {"type": "json_object"},
        })
        last_err = None
        for attempt in range(self.max_retries + 1):
            t0 = _time.monotonic()
            rec = {"driver": self.name, "model": self.model, "call_type": call_type,
                   "attempt": attempt + 1, "ok": False, "latency_ms": None,
                   "prompt_tokens": None, "completion_tokens": None, "fallback": False,
                   "error": None}
            retryable = False
            try:
                resp = httpx.post(
                    f"{self.base_url}/chat/completions",
                    headers={"Authorization": f"Bearer {self.api_key}"},
                    json=payload,
                    timeout=self.timeout,
                )
                rec["latency_ms"] = int((_time.monotonic() - t0) * 1000)
                rec["http_status"] = resp.status_code
                if resp.status_code in (401, 403):
                    raise LLMError(f"LLM 鉴权失败({resp.status_code})：检查 API Key")
                if resp.status_code == 429 or resp.status_code >= 500:
                    retryable = True
                    raise LLMError(f"LLM 服务暂不可用({resp.status_code})")
                resp.raise_for_status()
                data = resp.json()
                usage = data.get("usage") or {}
                rec["prompt_tokens"] = usage.get("prompt_tokens", usage.get("input_tokens"))
                rec["completion_tokens"] = usage.get("completion_tokens", usage.get("output_tokens"))
                content = data["choices"][0]["message"]["content"]
                parsed = json.loads(content)  # JSON 解析失败 => 可恢复，可重试
                if not isinstance(parsed, dict):
                    raise ValueError("模型必须返回 JSON 对象")
                rec["ok"] = True
                self.reports.append(rec)
                return parsed
            except LLMError as exc:
                last_err = exc
                rec["error"] = str(exc)[:200]
            except (json.JSONDecodeError, KeyError, IndexError) as exc:
                # 结构/JSON 可恢复错误
                retryable = True
                last_err = LLMError(f"LLM 返回结构异常: {str(exc)[:120]}")
                rec["error"] = str(last_err)[:200]
                rec["latency_ms"] = rec["latency_ms"] or int((_time.monotonic() - t0) * 1000)
            except Exception as exc:  # noqa: BLE001 - 网络/超时
                retryable = True
                last_err = LLMError(f"LLM 调用失败: {str(exc)[:200]}")
                rec["error"] = str(last_err)[:200]
                rec["latency_ms"] = rec["latency_ms"] or int((_time.monotonic() - t0) * 1000)
            self.reports.append(rec)
            if not retryable or attempt >= self.max_retries:
                break
        raise last_err or LLMError("LLM 调用失败")

    def _fallback_or_raise(self, kind: str, exc: Exception, fn, *args):
        if self.strict:
            raise LLMError(f"[strict] 模型调用失败且禁止降级：{str(exc)[:150]}") from exc
        # 记录一次"降级"事实（真实模型失败、改用本地规则）。
        self.reports.append({
            "driver": self.name, "model": self.model, "call_type": kind,
            "attempt": 0, "ok": False, "latency_ms": None,
            "prompt_tokens": None, "completion_tokens": None, "fallback": True,
            "error": f"降级到本地规则驱动: {str(exc)[:150]}",
        })
        return fn(*args)

    def plan(self, goal, scenario_cfg, slots):
        # 计划结构复杂且必须稳定：始终用本地确定性规划（非模型调用，不产生 report）。
        return self._fallback.plan(goal, scenario_cfg, slots)

    def next_action(self, task, ctx):
        system = (
            "你是 LongFlow 子任务执行器。只输出 JSON：{\"type\":\"tool_call|answer\","
            "\"tool\":...,\"args\":{...},\"reason\":...}。工具权限由系统控制，不要声称已执行。"
        )
        user = json.dumps(
            self._redact_payload(
                {"task": task.objective, "role": task.agent_role, "slots": task.slots,
                 "available_tools": ctx.get("tool_specs", ctx.get("available_tools", [])),
                 "tool_hint": ctx.get("tool_hint"), "goal": ctx.get("goal"),
                 "evidence": ctx.get("evidence", {})}),
            ensure_ascii=False,
        )
        try:
            out = self._chat_json(system, user, call_type="next_action")
            # 结构校验：类型合法；tool_call 必须带已知工具名与 dict 参数
            if out.get("type") == ACTION_TOOL_CALL:
                if not out.get("tool") or not isinstance(out.get("args", {}), dict):
                    raise LLMError("next_action 缺少 tool 或 args 结构非法")
                return out
            if out.get("type") == ACTION_ANSWER:
                return out
            raise LLMError(f"next_action 返回未知类型: {out.get('type')!r}")
        except LLMError as exc:
            return self._fallback_or_raise("next_action", exc,
                                           self._fallback.next_action, task, ctx)

    def draft_answer(self, question, chunks, slots):
        if not chunks:
            return self._fallback.draft_answer(question, chunks, slots)
        system = (
            "基于给定资料用中文回答，每条事实后标注 [cite:<chunk_id>]，"
            "资料不足就明确说无法确认，不得编造。只输出 JSON：{\"text\":...,\"citations\":[...]}。"
        )
        user = json.dumps(
            self._redact_payload(
                {"question": question,
                 "sources": [{"chunk_id": c["chunk_id"], "text": c["text"],
                              "doc": c["doc_name"], "citations": c.get("citations", [])}
                             for c in chunks]}),
            ensure_ascii=False,
        )
        try:
            out = self._chat_json(system, user, call_type="draft_answer")
            # 不自动补引用：模型漏写 citations 保持为空，由核验闸门判"证据不足"。
            if not isinstance(out.get("citations"), list):
                out["citations"] = []
            valid = {c["chunk_id"] for c in chunks}
            out["citations"] = [cid for cid in out["citations"] if cid in valid]
            if not isinstance(out.get("text"), str):
                raise LLMError("draft_answer 返回缺少 text 字段")
            out["no_evidence"] = len(out["citations"]) == 0
            return out
        except LLMError as exc:
            return self._fallback_or_raise("draft_answer", exc,
                                           self._fallback.draft_answer, question, chunks, slots)


def build_driver(cfg: dict) -> BaseDriver:
    llm_cfg = cfg.get("llm", {})
    driver = llm_cfg.get("driver", "local")
    if driver == "openai_compatible":
        strict = bool(llm_cfg.get("strict", False))
        try:
            return OpenAICompatibleDriver(
                llm_cfg.get("base_url", ""),
                llm_cfg.get("model", ""),
                llm_cfg.get("api_key", ""),
                timeout=float(llm_cfg.get("timeout_seconds", 60)),
                strict=strict,
                max_retries=int(llm_cfg.get("max_retries", 1)),
            )
        except LLMError:
            if strict:
                # 严格模式下缺 key/配置错误：显式失败，不静默降级
                raise
            return LocalDriver()
    return LocalDriver()
