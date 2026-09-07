"""LongFlow 回归评测共享工具（SPEC §10）。

本模块为测试与 ``tests/run_eval.py``（亦被 ``POST /api/eval/run`` 调用）提供
唯一的闭环运行与断言引擎。测试只针对 SPEC 契约编程，不依赖任何 LLM 打分：

    tmp_db()                      在临时目录建 SQLite，完整初始化（db init / RAG /
                                  插件加载 / 核心工具注册），返回 BackendSession。
    run_goal(db, goal, ...)       建 root 任务并循环 Engine.tick 到终态，
                                  返回 {root, events, approvals, tasks, steps}。
    restart(db_path, root_id)     模拟进程重启：新连接打开同一 DB（内存状态全丢），
                                  重新 tick 验证恢复。

后端入口点对齐实现（SPEC §1/§2/§4/§6/§8）：
    db.connect(db_path) / db.init_db(conn)
    config.load_config()（cfg["db_path"] / cfg["plugins"] / cfg["limits"]）
    plugins.loader.load_plugins(cfg) + collect_tools / collect_knowledge
    tools.ToolRegistry() + register_core_tools(reg) + ToolRuntime(conn, reg)
    orchestrator.Engine(conn, cfg, runtime).create_goal / tick / decide_approval
    rag.load_knowledge_path(conn, glob_pattern, scenario)
事件细节字段以后端为准（detail.tool / detail.ok / detail.idempotency_key）。
"""
from __future__ import annotations

import json
import math
import re
import sqlite3
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
TESTS_DIR = Path(__file__).resolve().parent
CASES_DIR = TESTS_DIR / "cases"

if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

TERMINAL = {"completed", "failed", "cancelled"}
CLARIFY_STATUS_HINTS = ("clarification", "clarify", "needs_info", "waiting_event")
BACKEND_MODULES = (
    "longflow.db", "longflow.rag", "longflow.orchestrator", "longflow.plugins.loader",
    "longflow.tools", "longflow.permissions", "longflow.events", "longflow.models",
    "longflow.llm", "longflow.config",
)

# ---------------------------------------------------------------------------
# 基础工具
# ---------------------------------------------------------------------------

def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def canonical_json(obj) -> str:
    """SPEC §1：幂等键与批准绑定用 canonical JSON（sort_keys + 无空白）。"""
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _load_module(name: str):
    try:
        return __import__(name, fromlist=["*"])
    except Exception:
        return None


def backend_available() -> bool:
    try:
        from longflow import orchestrator, tools  # noqa: F401
        return hasattr(orchestrator, "Engine") and hasattr(tools, "ToolRuntime")
    except Exception:
        return False


def backend_missing_reason() -> str:
    try:
        from longflow import orchestrator, tools  # noqa: F401
    except Exception as exc:
        return f"无法导入 longflow 后端（后端尚未实现？）：{type(exc).__name__}: {exc}"
    return "后端不完整（缺少 Engine/ToolRuntime）"


def skip_if_no_backend():
    """供 test 文件模块级调用：后端缺失时 skip 整模块。

    注意：函数名不能以 ``pytest_`` 开头，否则会被 pluggy 当作未知 hook 校验失败。
    """
    import pytest
    if not backend_available():
        pytest.skip(backend_missing_reason(), allow_module_level=True)


def load_case(case_id: str | None = None, path: str | Path | None = None) -> dict:
    import yaml
    if path is not None:
        p = Path(path)
    else:
        p = CASES_DIR / f"{case_id}.yaml"
    with open(p, "r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh)
    if not isinstance(data, dict) or "id" not in data:
        raise ValueError(f"用例文件缺少 id: {p}")
    data["_path"] = str(p)
    return data


def all_cases() -> list[dict]:
    if not CASES_DIR.exists():
        return []
    return [load_case(path=p) for p in sorted(CASES_DIR.glob("*.yaml"))]


def connect(db_path: str | Path) -> sqlite3.Connection:
    from longflow import db as db_mod
    return db_mod.connect(str(db_path))


def haversine_km(lon1, lat1, lon2, lat2) -> float:
    """独立复算球面距离（SPEC §8：geo 距离必须真实计算；测试不信任插件自报）。

    参数顺序为 (lon, lat) 以匹配 GeoJSON/工具输出。
    """
    r = 6371.0088
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


_TOKEN_RE = re.compile(r"[\w一-鿿]+", re.UNICODE)
_CJK_CHAR_RE = re.compile(r"[一-鿿]")
_STOPWORDS = {
    "the", "and", "for", "are", "with", "this", "that", "have", "from", "what",
    "when", "where", "which",
}


def _content_tokens(text: str) -> set[str]:
    """SPEC §4 词重叠核验：拉丁词按正则切；中文按字符 bigram（中文无空格）。"""
    toks = set()
    for t in _TOKEN_RE.findall(text or ""):
        t = t.lower()
        if _CJK_CHAR_RE.search(t):
            chars = [c for c in t if _CJK_CHAR_RE.match(c)]
            for i in range(len(chars) - 1):
                toks.add(chars[i] + chars[i + 1])
        elif len(t) >= 2 and t not in _STOPWORDS:
            toks.add(t)
    return toks


def text_overlap_ratio(claim: str, source: str) -> float:
    """SPEC §4：简单词重叠核验——论断词被引用 chunk 覆盖的比例。"""
    ct = _content_tokens(claim)
    if not ct:
        return 0.0
    st = _content_tokens(source)
    return len(ct & st) / len(ct)


# ---------------------------------------------------------------------------
# 后端会话
# ---------------------------------------------------------------------------

class BackendUnavailable(RuntimeError):
    pass


class BackendSession:
    """一个临时 DB 上的完整后端句柄。

    引导顺序对齐 longflow.api.AppState（SPEC §7）：
      connect → init_db → ToolRegistry + register_core_tools → load_plugins(cfg)
      → collect_tools 注册（含 data_dir/config）→ collect_knowledge 入库
      → ToolRuntime；场景知识在 create_root 时按 scenario 幂等加载。
    """

    def __init__(self, db_path: str | Path, plugins_overrides: dict | None = None,
                 init: bool = True):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.notes: list[str] = []
        self.plugins_overrides = dict(plugins_overrides or {})

        self.db_mod = _load_module("longflow.db")
        self.rag_mod = _load_module("longflow.rag")
        self.orc_mod = _load_module("longflow.orchestrator")
        self.tools_mod = _load_module("longflow.tools")
        self.perm_mod = _load_module("longflow.permissions")
        self.events_mod = _load_module("longflow.events")
        self.loader_mod = _load_module("longflow.plugins.loader")
        self.cfg_mod = _load_module("longflow.config")
        missing = [n for n in BACKEND_MODULES if _load_module(n) is None]
        if not backend_available():
            raise BackendUnavailable(backend_missing_reason() + f"；缺失模块: {missing}")

        self.conn = None
        self.registry = None
        self.runtime = None
        self.cfg = None
        self._plugins = []
        self.root_scenario = ""
        if init:
            self._bootstrap()

    # -- 引导 -------------------------------------------------------------
    def _bootstrap(self):
        cfg = self.cfg_mod.load_config()
        cfg["db_path"] = str(self.db_path)
        for name, enabled in self.plugins_overrides.items():
            entry = cfg.setdefault("plugins", {}).setdefault(name, {})
            entry["enabled"] = bool(enabled)
        self.cfg = cfg

        self.conn = self.db_mod.connect(str(self.db_path))
        self.db_mod.init_db(self.conn)

        self.registry = self.tools_mod.ToolRegistry()
        self.tools_mod.register_core_tools(self.registry)

        self._plugins = self.loader_mod.load_plugins(cfg)
        for tool_spec, lp in self.loader_mod.collect_tools(self._plugins):
            plugin_cfg = (cfg.get("plugins", {}).get(lp.name, {}) or {}).get("config", {})
            self.registry.register(tool_spec, data_dir=lp.data_dir, config=plugin_cfg)
        for chunks, lp in self.loader_mod.collect_knowledge(self._plugins):
            try:
                self.rag_mod.load_plugin_knowledge(self.conn, chunks, scenario=f"plugin:{lp.name}")
            except Exception as exc:  # noqa: BLE001
                self.notes.append(f"插件知识加载失败({lp.name}): {exc}")
        self.notes.append(
            f"bootstrap ok: 插件 {[p.name for p in self._plugins]}, 工具 {len(self.registry.names())} 个")

        http_client = None
        try:
            import httpx
            http_client = httpx.Client(timeout=20)
        except Exception:  # noqa: BLE001
            http_client = None
        self.runtime = self.tools_mod.ToolRuntime(
            self.conn, self.registry,
            timeout_seconds=cfg.get("limits", {}).get("tool_timeout_seconds", 30),
            http=http_client,
        )
        # 预热：加载全部场景知识（幂等）
        try:
            if hasattr(self.rag_mod, "load_all"):
                self.rag_mod.load_all(self.conn)
        except Exception as exc:  # noqa: BLE001
            self.notes.append(f"知识预热失败: {exc}")

    def engine(self):
        return self.orc_mod.Engine(self.conn, self.cfg, self.runtime)

    def ensure_scenario_knowledge(self, scenario: str):
        """SPEC §4 / api._ensure_knowledge：场景知识首次加载（幂等）。"""
        already = self.conn.execute(
            "SELECT COUNT(*) AS n FROM knowledge_chunks WHERE scenario=?", (scenario,)
        ).fetchone()["n"]
        if already:
            return
        scenario_cfg = self.cfg_mod.load_scenario(scenario)
        for pattern in scenario_cfg.get("knowledge", []) or []:
            self.rag_mod.load_knowledge_path(self.conn, pattern, scenario)
        self.notes.append(f"已加载场景知识: {scenario}")

    # -- 插件开关（SPEC §8：启用/禁用由配置 plugins.<name>.enabled） ----------
    def disable_plugin(self, name: str) -> str:
        """以禁用指定插件的 cfg 重建注册表/运行时，返回方式说明。"""
        self.cfg.setdefault("plugins", {}).setdefault(name, {})["enabled"] = False
        registry = self.tools_mod.ToolRegistry()
        self.tools_mod.register_core_tools(registry)
        plugins = self.loader_mod.load_plugins(self.cfg)
        for tool_spec, lp in self.loader_mod.collect_tools(plugins):
            plugin_cfg = (self.cfg.get("plugins", {}).get(lp.name, {}) or {}).get("config", {})
            registry.register(tool_spec, data_dir=lp.data_dir, config=plugin_cfg)
        self.registry = registry
        self.runtime = self.tools_mod.ToolRuntime(
            self.conn, registry,
            timeout_seconds=self.cfg.get("limits", {}).get("tool_timeout_seconds", 30),
            http=getattr(self.runtime, "http", None),
        )
        self._plugins = plugins
        return f"rebuild-registry(cfg.plugins.{name}.enabled=false)"

    # -- 注册表/运行时 ------------------------------------------------------
    def tool_names(self) -> list[str]:
        return self.registry.names()

    def call_tool(self, tool_name: str, args: dict, task_id: str, root_id: str,
                  scenario: str = ""):
        """SPEC §2 唯一调用路径 runtime.call(tool, args, task)。

        返回 (ok, value)；PermissionDenied/ApprovalRequired 以 ok=False + 结构化
        dict 返回（事件由运行时写入，不吞掉）。
        """
        task = _LiteTask(task_id, root_id, scenario)
        try:
            result = self.runtime.call(tool_name, args, task)
            return True, result
        except Exception as exc:  # noqa: BLE001
            return False, {
                "error_type": type(exc).__name__,
                "error": str(exc),
                "approval_id": getattr(exc, "approval_id", None),
                "tool": getattr(exc, "tool_name", tool_name),
            }

    # -- 事件/任务 ----------------------------------------------------------
    def events(self, root_id: str | None = None) -> list[dict]:
        rows = self.conn.execute(
            "SELECT id, ts, task_id, kind, actor, detail_json FROM events ORDER BY id").fetchall()
        out = []
        for r in rows:
            d = dict(r)
            try:
                d["detail"] = json.loads(d.get("detail_json") or "{}")
            except Exception:
                d["detail"] = {}
            out.append(d)
        return out

    def _row_to_task(self, r) -> dict:
        d = dict(r)
        for k, default in (("slots_json", {}), ("plan_json", {}),
                            ("result_json", {}), ("depends_on_json", [])):
            try:
                d[k[:-5]] = json.loads(d.get(k) or json.dumps(default, ensure_ascii=False))
            except Exception:
                d[k[:-5]] = default
        return d

    def task(self, task_id: str) -> dict | None:
        r = self.conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
        return self._row_to_task(r) if r else None

    def tasks_of_root(self, root_id: str) -> list[dict]:
        rows = self.conn.execute(
            "SELECT * FROM tasks WHERE root_id=? ORDER BY created_at, id", (root_id,)).fetchall()
        return [self._row_to_task(r) for r in rows]

    # -- 目标/审批/授权 ------------------------------------------------------
    def create_root(self, goal: str, scenario: str, slots: dict | None = None,
                    title: str | None = None) -> str:
        """建 root 任务（Engine.create_goal 内含入口闸门；clarify 时 waiting_event）。"""
        self.ensure_scenario_knowledge(scenario)
        res = self.engine().create_goal(goal, scenario, slots or {})
        self.root_scenario = scenario
        return res["task_id"]

    def tick(self, root_id: str) -> dict:
        return self.engine().tick(root_id)

    def pending_approvals(self, root_id: str) -> list[dict]:
        rows = self.conn.execute(
            "SELECT * FROM approvals WHERE status='pending' AND task_id IN "
            "(SELECT id FROM tasks WHERE root_id=?) ORDER BY created_at", (root_id,)).fetchall()
        return [dict(r) for r in rows]

    def approvals(self, root_id: str) -> list[dict]:
        rows = self.conn.execute(
            "SELECT * FROM approvals WHERE task_id IN "
            "(SELECT id FROM tasks WHERE root_id=?) ORDER BY created_at", (root_id,)).fetchall()
        return [dict(r) for r in rows]

    def add_grant(self, scope: str, tool_name: str | None = None, action: str | None = None,
                  object_pattern: str | None = None, max_count: int | None = None,
                  expires_at: str | None = None, granted_by: str = "test_setup",
                  task_id: str | None = None) -> str:
        """写 grants 表（SPEC §1）。优先 permissions.add_grant；deny/额外字段直接插表。"""
        if self.perm_mod is not None and action is None:
            try:
                return self.perm_mod.add_grant(
                    self.conn, scope=scope, tool_name=tool_name,
                    object_pattern=object_pattern, max_count=max_count,
                    expires_at=expires_at, granted_by=granted_by, task_id=task_id)
            except TypeError:
                pass
        gid = "gr_" + uuid.uuid4().hex
        self.conn.execute(
            """INSERT INTO grants(id, scope, tool_name, action, object_pattern,
               max_count, used_count, expires_at, granted_by, task_id, created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (gid, scope, tool_name, action, object_pattern, max_count, 0,
             expires_at, granted_by, task_id, now_iso()))
        self.conn.commit()
        return gid

    def decide_approval(self, approval_id: str, decision: str = "approved",
                        args: dict | None = None, decided_by: str = "test_user") -> dict:
        """SPEC §3/§7：Engine.decide_approval 更新审批并立即 tick 恢复分支。

        高风险动作的参数绑定由 approvals.args_hash 在权限层校验（canonical JSON）；
        测试默认提交 approval 绑定参数（args 为 None 时即如此）。
        """
        try:
            res = self.engine().decide_approval(approval_id, decision, decided_by)
            return {"ok": True, "via": "Engine.decide_approval", "result": res}
        except FileNotFoundError:
            return {"ok": False, "reason": f"approval {approval_id} 不存在"}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "via": "Engine.decide_approval",
                    "error": f"{type(exc).__name__}: {exc}"}

    def user_message(self, root_id: str, text: str, slot_updates: dict | None = None) -> dict:
        return self.engine().user_message(root_id, text, slot_updates)

    def run_root(self, root_id: str) -> dict:
        """SPEC §6：run_root 可重入、可恢复（实现中命名为 Engine.tick）。"""
        return self.engine().tick(root_id)

    def close(self):
        try:
            if self.conn is not None:
                self.conn.close()
        except Exception:  # noqa: BLE001
            pass


class _LiteTask:
    """ToolRuntime.call 需要的最小 task 形状（id/root_id/agent_role/slots）。"""

    def __init__(self, task_id: str, root_id: str, scenario: str = ""):
        self.id = task_id
        self.root_id = root_id
        self.agent_role = "executor"
        self.slots = {"__scenario__": scenario}
        self.scenario = scenario


# ---------------------------------------------------------------------------
# 高层助手
# ---------------------------------------------------------------------------

def tmp_db(tmp_path=None, plugins_overrides: dict | None = None) -> BackendSession:
    """在临时目录建 SQLite 并完整初始化（SPEC §1/§2/§4/§8）。"""
    import tempfile
    if tmp_path is None:
        tmp_path = Path(tempfile.mkdtemp(prefix="longflow_eval_"))
    db_path = Path(tmp_path) / "longflow.db"
    return BackendSession(db_path, plugins_overrides=plugins_overrides)


def _drive(session: BackendSession, root_id: str, max_steps: int = 60) -> dict:
    """循环 tick 直到终态/等待外部事件（SPEC §6 调度循环，可重入）。"""
    steps = 0
    last_error = None
    while steps < max_steps:
        root = session.task(root_id)
        if root is None:
            last_error = "root 任务丢失"
            break
        if root["status"] in TERMINAL:
            break
        try:
            session.tick(root_id)
        except Exception as exc:  # noqa: BLE001
            last_error = f"{type(exc).__name__}: {exc}"
        steps += 1
        root = session.task(root_id)
        if root["status"] in TERMINAL:
            break
        if root["status"] == "waiting_event":
            break
        children = [t for t in session.tasks_of_root(root_id) if t["id"] != root_id]
        # 任一子任务在等待审批/外部事件 → 交回外部（approve/restart 步骤）推进，
        # 不继续 tick（等待时无模型空转，SPEC §6）。
        if children and any(t["status"] in ("waiting_approval", "waiting_event")
                            for t in children):
            break
        # 没有可推进的子任务（全部终态但 root 未终态，tick 内部负责 gate_exit）→
        # 再 tick 一次让出口闸门定稿，然后停止。
        if children and all(t["status"] in ("completed", "failed", "cancelled")
                            for t in children):
            session.tick(root_id)
            break
    return {"steps": steps, "last_error": last_error}


def run_goal(db, goal: str, scenario: str, slots: dict | None = None,
             grants: list[dict] | None = None, max_steps: int = 60) -> dict:
    """建 root 任务并循环 tick 直到终态。

    返回 {root_id, root, events, approvals, tasks, steps, last_error, notes}。
    注：场景 preauth 由 Engine.create_goal 按 policies 自动落地；grants 用于
    注入 deny / 额外授权。
    """
    for g in grants or []:
        db.add_grant(**g)
    root_id = db.create_root(goal, scenario, slots=slots)
    drive = _drive(db, root_id, max_steps=max_steps)
    root = db.task(root_id)
    return {
        "root_id": root_id,
        "root": root,
        "events": db.events(root_id),
        "approvals": db.approvals(root_id),
        "tasks": db.tasks_of_root(root_id),
        "steps": drive["steps"],
        "last_error": drive["last_error"],
        "notes": list(db.notes),
    }


def restart(db_path, root_id: str, max_steps: int = 60, drive: bool = True) -> BackendSession:
    """SPEC §6/§1：模拟进程重启——新连接重新打开同一 DB 文件（内存状态全丢）。

    drive=True 时立即循环 tick 到终态/等待点（恢复循环）；flow 编排用 drive=False
    只完成"进程重启"动作，由后续显式 tick/approve 步骤推进，避免在审批待决时
    误推进。
    """
    session = BackendSession(Path(db_path), init=True)
    if drive:
        _drive(session, root_id, max_steps=max_steps)
    return session


# ---------------------------------------------------------------------------
# pytest fixtures
# ---------------------------------------------------------------------------

import pytest  # noqa: E402


def pytest_configure(config):
    config.addinivalue_line("markers", "eval: SPEC §10 回归评测用例")


@pytest.fixture
def workdir():
    """沙箱安全的临时工作目录：工作区内 .lfwork/<uuid>/。

    pytest 内置 tmp_path 工厂在受限沙箱下会扫描系统临时目录并触发 PermissionError；
    run_all/main（API 进程）路径仍用 tempfile。
    """
    d = REPO_ROOT / ".lfwork" / uuid.uuid4().hex
    d.mkdir(parents=True, exist_ok=True)
    yield d


@pytest.fixture
def db(workdir):
    """临时 DB 上的完整后端会话。"""
    session = tmp_db(workdir)
    yield session
    session.close()


@pytest.fixture
def tmp_path_db(workdir):
    return workdir
