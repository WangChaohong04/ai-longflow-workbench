/* LongFlow 长程任务工作台 —— 零构建、零依赖原生 ES Module
 * 仅通过 fetch 调用 /api/*；所有渲染均做防御式取值。 */
import { getActiveMapPluginId, listMapPlugins, setActiveMapPlugin, getManualMapPlugin } from "./maps/index.js";
import { createMapController } from "./maps/panel.js";

// ============================== 全局状态 ==============================
const state = {
  view: "tasks",          // tasks | new | detail | plugins | eval
  detailId: null,
  config: null,
  pollTimer: null,
  pollInFlight: false,
  healthTimer: null,
  clarificationDrafts: new Map(),
  mapPanels: new Map(),  // geoKey -> 地图面板缓存（plugin/host/模式/选中点/路线方式）
};

const TERMINAL_STATUS = new Set(["completed", "failed", "cancelled"]);

// ============================== 中文映射 ==============================
const STATUS_LABELS = {
  pending: "待处理",
  ready: "就绪",
  in_progress: "进行中",
  waiting_approval: "待审批",
  waiting_event: "等待中",
  completed: "已完成",
  failed: "失败",
  cancelled: "已取消",
};
const STATUS_BADGE = {
  pending: "",
  ready: "info",
  in_progress: "accent pulse",
  waiting_approval: "warn pulse",
  waiting_event: "warn pulse",
  completed: "ok",
  failed: "danger",
  cancelled: "",
};
const ROLE_LABELS = {
  controller: "主控",
  researcher: "检索",
  executor: "执行",
  verifier: "核验",
};
const EVENT_LABELS = {
  task_created: "任务创建",
  task_status: "状态变更",
  tool_request: "工具请求",
  tool_result: "工具结果",
  tool_denied: "越权拦截",
  approval_requested: "请求审批",
  approval_decided: "审批决策",
  gate_entry: "入口闸门",
  gate_exit: "出口闸门",
  message: "用户消息",
  llm_call: "模型调用",
  error: "错误",
  recovery: "恢复",
};
const ERROR_CODE_HINTS = {
  permission_denied: "该操作因权限不足被阻止（越权拦截）。",
  approval_required: "这个动作需要人工审批后才会执行。",
  clarification_needed: "信息还不够，需要你补充一些内容后任务才能继续。",
  dependency_failed: "它依赖的前置任务失败了，所以本任务无法继续。",
  budget_exceeded: "任务执行步数或模型调用次数已达上限。",
  not_found: "没有找到对应的记录。",
  plugin_error: "某个插件加载或运行出错，核心功能不受影响。",
};
const SOURCE_LABELS = {
  local_geojson: "本地样例数据",
  mock: "模拟数据",
};
const VERDICT_TEXT = {
  approved: "已批准",
  rejected: "已拒绝",
  pending: "待审批",
};

// ============================== 小工具 ==============================
function $(sel, root = document) { return root.querySelector(sel); }

function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (v == null || v === false) continue;
    if (k === "class") node.className = v;
    else if (k === "html") node.innerHTML = v;
    else if (k.startsWith("on") && typeof v === "function") node.addEventListener(k.slice(2).toLowerCase(), v);
    else if (k === "dataset") Object.assign(node.dataset, v);
    else node.setAttribute(k, v === true ? "" : v);
  }
  for (const c of children.flat()) {
    if (c == null || c === false) continue;
    node.appendChild(typeof c === "string" || typeof c === "number" ? document.createTextNode(c) : c);
  }
  return node;
}

function escapeHtml(s) {
  return String(s ?? "").replace(/[&<>"']/g, (m) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[m]));
}

function fmtTime(iso) {
  if (!iso) return "—";
  const d = new Date(iso);
  if (isNaN(d.getTime())) return iso;
  const p = (n) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`;
}

function badge(text, kind = "") {
  return el("span", { class: `badge ${kind} dot` }, text);
}

function statusBadge(status) {
  return badge(STATUS_LABELS[status] || status || "未知", STATUS_BADGE[status] || "");
}

function parseJson(maybe, fallback) {
  if (maybe == null) return fallback;
  if (typeof maybe === "object") return maybe;
  try { return JSON.parse(maybe); } catch { return fallback; }
}

function asArray(v) { return Array.isArray(v) ? v : []; }

function roleLabel(role) { return ROLE_LABELS[role] || role || "—"; }

// ============================== API 封装 ==============================
class ApiError extends Error {
  constructor(message, code, status, data) {
    super(message);
    this.code = code;
    this.status = status;
    this.data = data;
  }
}

async function api(path, options = {}) {
  let resp;
  try {
    resp = await fetch(path, {
      headers: options.body ? { "Content-Type": "application/json" } : undefined,
      ...options,
    });
  } catch (e) {
    throw new ApiError("无法连接后端服务（网络错误或服务未启动）。", "network", 0);
  }
  let data = null;
  const text = await resp.text();
  if (text) {
    try { data = JSON.parse(text); } catch { data = { raw: text }; }
  }
  if (!resp.ok) {
    const code = (data && data.code) || `http_${resp.status}`;
    const msg = (data && data.error) || `请求失败（HTTP ${resp.status}）`;
    throw new ApiError(msg, code, resp.status, data);
  }
  return data;
}

function errorHint(err) {
  if (err instanceof ApiError) {
    const hint = ERROR_CODE_HINTS[err.code];
    return hint ? `${hint}（${err.message}）` : err.message;
  }
  return String(err && err.message ? err.message : err);
}

// ============================== 提示条 ==============================
function toast(message, kind = "info", title) {
  const wrap = $("#toast-wrap");
  const t = el("div", { class: `toast ${kind}` },
    title ? el("div", { class: "t-title" }, title) : null,
    el("div", { class: "t-msg" }, message));
  wrap.appendChild(t);
  setTimeout(() => {
    t.style.transition = "opacity .4s, transform .4s";
    t.style.opacity = "0";
    t.style.transform = "translateX(14px)";
    setTimeout(() => t.remove(), 420);
  }, 6000);
}

// ============================== 健康检查 ==============================
async function checkHealth() {
  const node = $("#health");
  try {
    await api("/api/health");
    node.className = "health ok";
    $("#health-text").textContent = "后端已连接";
    return true;
  } catch {
    node.className = "health bad";
    $("#health-text").textContent = "后端未连接";
    return false;
  }
}

// ============================== 导航 ==============================
function setView(view, opts = {}) {
  stopPolling();
  state.view = view;
  state.detailId = opts.detailId || null;
  document.querySelectorAll(".tab").forEach((t) => {
    const v = t.dataset.view;
    t.classList.toggle("active", v === view || (view === "detail" && v === "tasks"));
  });
  render();
}

function render() {
  const app = $("#app");
  app.innerHTML = "";
  if (state.view === "tasks") renderTasks(app);
  else if (state.view === "new") renderNewTask(app);
  else if (state.view === "detail") renderDetail(app);
  else if (state.view === "plugins") renderPlugins(app);
  else if (state.view === "eval") renderEval(app);
}

// ============================== 任务列表 ==============================
async function renderTasks(app) {
  app.appendChild(el("p", { class: "loading" }, "加载任务中…"));
  let tasks = [];
  try {
    const data = await api("/api/tasks");
    tasks = normalizeTaskList(data);
  } catch (err) {
    app.innerHTML = "";
    app.appendChild(el("div", { class: "empty" },
      el("div", { class: "big" }, "任务加载失败"),
      el("div", {}, errorHint(err)),
      el("div", { style: "margin-top:12px" }, el("button", { class: "btn", onClick: () => render() }, "重试"))));
    return;
  }
  app.innerHTML = "";
  const roots = tasks.filter((t) => t.kind === "goal" || !t.parent_id);
  const list = roots.length ? roots : tasks;
  list.sort((a, b) => String(b.created_at || "").localeCompare(String(a.created_at || "")));

  const head = el("div", { class: "page-head" },
    el("h1", {}, "任务列表"),
    el("span", { class: "sub" }, list.length ? `共 ${list.length} 个目标任务` : "还没有任务"),
    el("span", { class: "spacer" }),
    el("button", { class: "btn sm", onClick: () => render() }, "刷新"),
    el("button", { class: "btn sm primary", onClick: () => setView("new") }, "＋ 新建任务"));
  app.appendChild(head);

  if (!list.length) {
    app.appendChild(el("div", { class: "empty" },
      el("div", { class: "big" }, "还没有任何任务"),
      el("div", {}, "点击「新建任务」，写下你的目标，LongFlow 会拆解、检索、执行并核验。")));
    return;
  }

  const wrap = el("div", { class: "task-list" });
  for (const t of list) {
    wrap.appendChild(el("button", { class: "task-item", onClick: () => setView("detail", { detailId: t.id }) },
      el("div", { class: "ti-main" },
        el("div", { class: "ti-title", title: t.objective || t.title }, t.objective || t.title),
        el("div", { class: "ti-meta" },
          el("span", {}, `场景：${t.scenario || "默认"}`),
          el("span", {}, "创建于 " + fmtTime(t.created_at)))),
      el("div", { class: "ti-badges" },
        statusBadge(t.status))));
  }
  app.appendChild(wrap);
}

function normalizeTaskList(data) {
  if (Array.isArray(data)) return data;
  if (data && Array.isArray(data.tasks)) return data.tasks;
  if (data && Array.isArray(data.items)) return data.items;
  return [];
}

// ============================== 新建任务 ==============================
async function renderNewTask(app) {
  app.appendChild(el("p", { class: "loading" }, "加载配置中…"));
  try {
    state.config = await api("/api/config");
  } catch (err) {
    app.innerHTML = "";
    app.appendChild(el("div", { class: "empty" },
      el("div", { class: "big" }, "配置加载失败"),
      el("div" , {}, errorHint(err)),
      el("div", { style: "margin-top:12px" }, el("button", { class: "btn", onClick: () => render() }, "重试"))));
    return;
  }
  app.innerHTML = "";
  const scenarios = normalizeScenarios(state.config);

  const head = el("div", { class: "page-head" },
    el("h1", {}, "新建任务"),
    el("span", { class: "sub" }, "写下目标，选择场景；系统会自动拆解、执行并把结果核验给你看。"));
  app.appendChild(head);

  const goalInput = el("textarea", { id: "f-goal", placeholder: "例如：帮我采购 2 台笔记本电脑，预算 12000 元以内；或：查一下公司采购审批规则" });
  const scenarioSelect = el("select", { id: "f-scenario" });
  for (const s of scenarios) {
    scenarioSelect.appendChild(el("option", { value: s.name }, `${s.name} — ${s.description || ""}`));
  }

  const slotBox = el("div", { class: "slot-editor" });
  const customSlots = [];

  function presetChips() {
    const sc = scenarios.find((s) => s.name === scenarioSelect.value);
    const box = el("div", { class: "slot-preset" });
    const defined = (sc && sc.slots || []).filter((s) => s.name);
    for (const s of defined) {
      box.appendChild(el("button", {
        type: "button", class: "chip",
        title: s.prompt || s.description || "",
        onClick: () => addSlotRow(s.name, "", s.prompt || s.description || s.name),
      }, `＋ ${s.name}${s.required_for && s.required_for.length ? " *" : ""}`));
    }
    return box;
  }

  function addSlotRow(k = "", v = "", ph = "") {
    const keyInput = el("input", { type: "text", class: "k", placeholder: "槽位名（如 item）", value: k });
    const valInput = el("input", { type: "text", placeholder: ph || "槽位值" });
    const row = el("div", { class: "slot-row" }, keyInput, valInput,
      el("button", { type: "button", class: "btn sm danger", onClick: () => { row.remove(); } }, "删除"));
    slotBox.appendChild(row);
    valInput.focus();
    customSlots.push({ key: keyInput, val: valInput, row });
  }

  const presetHolder = el("div");
  function refreshPresets() {
    presetHolder.innerHTML = "";
    presetHolder.appendChild(presetChips());
  }
  scenarioSelect.addEventListener("change", refreshPresets);
  refreshPresets();

  const submitBtn = el("button", { class: "btn primary", type: "submit" }, "提交任务");
  const form = el("form", {
    onSubmit: async (ev) => {
      ev.preventDefault();
      const goal = goalInput.value.trim();
      if (!goal) { toast("请先填写任务目标。", "warn", "目标为空"); goalInput.focus(); return; }
      const slots = {};
      for (const s of customSlots) {
        if (!s.row.isConnected) continue;
        const k = s.key.value.trim();
        const v = s.val.value.trim();
        if (k && v) slots[k] = v;
      }
      submitBtn.disabled = true;
      submitBtn.textContent = "提交中…";
      try {
        const body = { goal, scenario: scenarioSelect.value || undefined, slots: Object.keys(slots).length ? slots : undefined };
        const res = await api("/api/tasks", { method: "POST", body: JSON.stringify(body) });
        const newId = res && (res.id || res.task_id || (res.task && res.task.id));
        toast("任务已创建，系统开始处理。", "success", "已提交");
        if (newId) setView("detail", { detailId: newId });
        else setView("tasks");
      } catch (err) {
        toast(errorHint(err), "error", "提交失败");
        submitBtn.disabled = false;
        submitBtn.textContent = "提交任务";
      }
    },
  },
    el("div", { class: "card" },
      el("div", { class: "form-row" },
        el("label", {}, "任务目标", el("span", { class: "req" }, "*")),
        goalInput),
      el("div", { class: "form-row" },
        el("label", {}, "场景", el("span", { class: "help" }, "不同场景有不同的知识、工具与规则")),
        scenarioSelect),
      el("div", { class: "form-row" },
        el("label", {}, "补充信息（槽位，可选）",
          el("span", { class: "help" }, "把已知信息填上可减少来回追问；标 * 为某些意图的必填项")),
        slotBox,
        el("div", { style: "margin-top:8px" },
          el("button", { type: "button", class: "btn sm", onClick: () => addSlotRow() }, "＋ 添加一项")),
        presetHolder),
      el("div", { class: "row-gap", style: "margin-top:6px" },
        submitBtn,
        el("button", { type: "button", class: "btn", onClick: () => setView("tasks") }, "返回列表"))));

  app.appendChild(form);
}

function normalizeScenarios(cfg) {
  if (!cfg) return [];
  let list = cfg.scenarios;
  if (!Array.isArray(list)) {
    if (list && typeof list === "object") {
      list = Object.entries(list).map(([name, v]) => (v && typeof v === "object" ? { name, ...v } : { name, description: String(v) }));
    } else list = [];
  }
  return list.map((s) => {
    if (typeof s === "string") return { name: s, description: "", slots: [] };
    let slots = s.slots || [];
    if (slots && !Array.isArray(slots) && typeof slots === "object") {
      slots = Object.entries(slots).map(([name, v]) => (v && typeof v === "object" ? { name, ...v } : { name, prompt: String(v) }));
    }
    return { name: s.name || s.id, description: s.description || s.desc || "", slots: Array.isArray(slots) ? slots : [] };
  }).filter((s) => s.name);
}

// ============================== 任务详情 ==============================
async function renderDetail(app, silent = false) {
  if (!silent) app.appendChild(el("p", { class: "loading" }, "加载详情中…"));
  let detail;
  try {
    detail = await api(`/api/tasks/${encodeURIComponent(state.detailId)}`);
  } catch (err) {
    if (!silent) {
      app.innerHTML = "";
      app.appendChild(el("div", { class: "empty" },
        el("div", { class: "big" }, "详情加载失败"),
        el("div", {}, errorHint(err)),
        el("div", { style: "margin-top:12px" },
          el("button", { class: "btn", onClick: () => setView("tasks") }, "返回列表"),
          " ",
          el("button", { class: "btn", onClick: () => render() }, "重试"))));
    }
    return;
  }
  if (state.view !== "detail") return;
  // Do not replace an active editor, including an IME composition.
  if (silent && app.contains(document.activeElement) &&
      document.activeElement.matches("input, textarea, select, [contenteditable]")) return;
  const d = normalizeDetail(detail);

  // 终态停止轮询
  if (TERMINAL_STATUS.has(d.root.status)) stopPolling();
  else startPolling();

  app.innerHTML = "";

  // 顶部
  const head = el("div", { class: "page-head" },
    el("button", { class: "btn sm", onClick: () => setView("tasks") }, "← 返回"),
    el("h1", { style: "font-size:18px" }, "任务详情"),
    el("span", { class: "spacer" }),
    statusBadge(d.root.status),
    el("span", { class: "badge" }, `场景：${d.root.scenario || "默认"}`),
    el("button", { class: "btn sm", onClick: () => renderDetail(app, true) }, "刷新"),
    !TERMINAL_STATUS.has(d.root.status)
      ? el("button", {
          class: "btn sm danger",
          onClick: async () => {
            if (!confirm("确定取消这个任务吗？已发生的操作会在结果中说明。")) return;
            try {
              await api(`/api/tasks/${encodeURIComponent(d.root.id)}/cancel`, { method: "POST" });
              toast("任务已取消。", "success");
              renderDetail(app, true);
            } catch (err) { toast(errorHint(err), "error", "取消失败"); }
          },
        }, "取消任务")
      : null);
  app.appendChild(head);

  // —— 目标与约束 ——
  const goalCard = el("div", { class: "card tinted" },
    el("h2", {}, "目标", el("span", { class: "hint" }, "这个任务要达成什么")),
    el("div", { style: "font-size:16px;font-weight:600;line-height:1.7" }, d.root.objective || d.root.title));
  const slots = d.slots;
  if (slots && Object.keys(slots).length) {
    const grid = el("div", { class: "kv-grid" });
    for (const [k, v] of Object.entries(slots)) {
      grid.appendChild(el("div", { class: "kv-item" },
        el("span", { class: "k" }, k + "："),
        el("span", { class: "v" }, typeof v === "object" ? JSON.stringify(v) : String(v))));
    }
    goalCard.appendChild(el("div", { style: "margin-top:12px" },
      el("div", { class: "muted", style: "font-size:12.5px;margin-bottom:6px" }, "已知约束 / 补充信息"),
      grid));
  }
  app.appendChild(goalCard);

  // —— 待决策区 ——
  renderDecisions(app, d);

  // —— 子任务树 ——
  const treeCard = el("div", { class: "card" },
    el("h2", {}, "执行进度",
      el("span", { class: "hint" }, "系统把目标拆成子任务，按依赖关系推进")));
  treeCard.appendChild(buildTree(d));
  app.appendChild(treeCard);

  // —— 结果区 ——
  renderResult(app, d);

  // —— GEO 地图 ——
  const geos = collectGeo(d);
  geos.forEach((g, i) => app.appendChild(buildGeoCard(g, `${d.root.id}:geo:${i}`)));

  // —— 事件时间线 ——
  const evCard = el("div", { class: "card" },
    el("h2", {}, "动作记录",
      el("span", { class: "hint" }, `共 ${d.events.length} 条，只记录动作与事实`)));
  evCard.appendChild(buildTimeline(d.events));
  app.appendChild(evCard);
}

function normalizeDetail(detail) {
  const root = detail.task || detail.root || detail;
  let subtasks = asArray(detail.subtasks || detail.children || detail.tasks);
  if (!subtasks.length && root && Array.isArray(root.subtasks)) subtasks = root.subtasks;
  const approvals = asArray(detail.approvals || root.approvals);
  const events = asArray(detail.events || root.events);
  const slots = parseJson(root.slots_json ?? root.slots, {}) || {};
  const result = parseJson(root.result_json ?? root.result, {}) || {};
  const plan = parseJson(root.plan_json ?? root.plan, {}) || {};
  const norm = (t) => ({
    ...t,
    slots: parseJson(t.slots_json ?? t.slots, {}) || {},
    result: parseJson(t.result_json ?? t.result, {}) || {},
    depends_on: asArray(t.depends_on_json ?? t.depends_on ?? t.deps).map(String),
  });
  return {
    root: norm(root),
    subtasks: subtasks.map(norm),
    approvals,
    events: events.map((e) => ({ ...e, detail: parseJson(e.detail_json ?? e.detail, {}) || {} })),
    slots,
    result,
    plan,
  };
}

// ---------- 待决策 ----------
function renderDecisions(app, d) {
  const pendingApprovals = d.approvals.filter((a) => (a.status || "pending") === "pending");
  const clarifies = collectClarifications(d);

  if (!pendingApprovals.length && !clarifies.length) {
    // 仍展示已处理审批的结果（简要）
    const decided = d.approvals.filter((a) => a.status && a.status !== "pending");
    if (decided.length) {
      const card = el("div", { class: "card" },
        el("h2", {}, "审批记录"));
      for (const a of decided) {
        card.appendChild(el("div", { class: "row-gap", style: "padding:6px 0;font-size:13.5px" },
          badge(VERDICT_TEXT[a.status] || a.status, a.status === "approved" ? "ok" : a.status === "rejected" ? "danger" : "warn"),
          el("strong", {}, a.tool_name || "工具"),
          el("span", { class: "muted" }, a.decided_by ? `决策人：${a.decided_by}` : ""),
          el("span", { class: "muted" }, fmtTime(a.decided_at || a.created_at))));
      }
      app.appendChild(card);
    }
    return;
  }

  const card = el("div", { class: "card" },
    el("h2", {}, "需要你决定",
      el("span", { class: "hint" }, "处理后任务会自动继续；等待不影响其他分支执行")));

  for (const a of pendingApprovals) {
    const args = parseJson(a.args_json ?? a.args, {}) || {};
    const box = el("div", { class: "decision-card" },
      el("div", { class: "dc-title" },
        el("span", { class: "badge warn pulse" }, "待审批"),
        el("span", {}, "工具："),
        el("span", { class: "mono" }, a.tool_name || "未知工具")),
      a.reason ? el("div", { class: "dc-reason" }, "原因：" + a.reason) : null,
      el("div", { class: "dc-args" }, "绑定参数：\n" + JSON.stringify(args, null, 2)),
      el("div", { class: "dc-actions" },
        el("button", {
          class: "btn ok",
          onClick: async (ev) => decideApproval(ev, a.id, "approved"),
        }, "批准执行"),
        el("button", {
          class: "btn danger",
          onClick: async (ev) => decideApproval(ev, a.id, "rejected"),
        }, "拒绝")));
    card.appendChild(box);
  }

  for (const c of clarifies) {
    const draftKey = `${d.root.id}:${c.name || c.question}`;
    const input = el("input", { type: "text", placeholder: "在这里输入你的回答…" });
    input.value = state.clarificationDrafts.get(draftKey) || "";
    input.addEventListener("input", () => state.clarificationDrafts.set(draftKey, input.value));
    const box = el("div", { class: "decision-card clarify" },
      el("div", { class: "dc-title" },
        el("span", { class: "badge info pulse" }, "待补充信息"),
        el("span", {}, "系统有个问题")),
      el("div", { class: "dc-reason" }, c.question),
      input,
      el("div", { class: "dc-actions" },
        el("button", {
          class: "btn primary",
          onClick: async (ev) => {
            const text = input.value.trim();
            if (!text) { toast("请先填写你的回答。", "warn"); return; }
            const btn = ev.target;
            btn.disabled = true;
            try {
              await api(`/api/tasks/${encodeURIComponent(d.root.id)}/message`, {
                method: "POST",
                body: JSON.stringify({ text, ...(c.name ? { slots: { [c.name]: text } } : {}) }),
              });
              state.clarificationDrafts.delete(draftKey);
              toast("已提交，任务继续。", "success");
              render();
            } catch (err) {
              toast(errorHint(err), "error", "提交失败");
              btn.disabled = false;
            }
          },
        }, "提交回答")));
    card.appendChild(box);
  }
  app.appendChild(card);
}

async function decideApproval(ev, approvalId, decision) {
  const btn = ev.target;
  btn.disabled = true;
  try {
    await api(`/api/approvals/${encodeURIComponent(approvalId)}/decide`, {
      method: "POST",
      body: JSON.stringify({ decision }),
    });
    toast(decision === "approved" ? "已批准，任务将继续执行。" : "已拒绝该操作。", "success");
    render();
  } catch (err) {
    toast(errorHint(err), "error", "操作失败");
    btn.disabled = false;
  }
}

function collectClarifications(d) {
  const out = [];
  const seen = new Set();
  const push = (q, name) => {
    const question = String(q || "").trim();
    if (question && !seen.has(question)) { seen.add(question); out.push({ question, name }); }
  };
  // 1) result / slots 中的结构化 clarify
  for (const r of [d.result, ...d.subtasks.map((t) => t.result)]) {
    if (!r) continue;
    if (r.clarify) {
      const c = r.clarify;
      if (typeof c === "string") push(c);
      else if (Array.isArray(c)) c.forEach((x) => push(typeof x === "string" ? x : x.question || x.prompt || x.field, typeof x === "object" ? x.name || x.field : undefined));
      else if (typeof c === "object") {
        if (c.question || c.prompt) push(c.question || c.prompt);
        asArray(c.missing_fields || c.missing || c.fields).forEach((f) =>
          push(`请补充「${typeof f === "string" ? f : f.name || f.field}」${(f && f.prompt) ? `：${f.prompt}` : ""}`));
      }
    }
    if (Array.isArray(r.questions)) r.questions.forEach((q) => push(typeof q === "string" ? q : q.question));
  }
  // 2) 事件中的 clarification / message 线索
  for (const e of d.events) {
    if (e.kind === "message" && e.detail && e.detail.awaiting_reply) push(e.detail.text || e.detail.message);
    const det = e.detail || {};
    if (det.need_clarification || det.clarification_needed) {
      push(det.question || det.message || det.reason);
      asArray(det.missing_fields || det.missing).forEach((f) => push(`请补充「${typeof f === "string" ? f : f.name || f.field}」`));
    }
  }
  // 3) 状态为 waiting_event 且有 clarify 信号
  if (d.root.status === "waiting_event") {
    const c = d.result && d.result.clarify;
    if (!c) push("任务正在等待补充信息或外部事件，请在下方回答或稍后查看。");
  }
  return out;
}

// ---------- 子任务树 ----------
function buildTree(d) {
  const all = [d.root, ...d.subtasks];
  const byId = new Map(all.map((t) => [String(t.id), t]));
  const childrenOf = new Map();
  for (const t of d.subtasks) {
    const pid = t.parent_id ? String(t.parent_id) : String(d.root.id);
    if (!childrenOf.has(pid)) childrenOf.set(pid, []);
    childrenOf.get(pid).push(t);
  }
  for (const arr of childrenOf.values()) {
    arr.sort((a, b) => String(a.created_at || "").localeCompare(String(b.created_at || "")) || String(a.id).localeCompare(String(b.id)));
  }

  function nodeEl(t, isRoot) {
    const deps = (t.depends_on || []).filter((id) => String(id) !== String(t.parent_id || d.root.id));
    const head = el("div", { class: "tn-head" },
      el("span", { class: `badge ${isRoot ? "accent" : ""}` }, isRoot ? "目标" : roleLabel(t.agent_role)),
      el("span", { class: "tn-title" }, isRoot ? (t.objective || t.title) : (t.title || t.objective)),
      el("span", { class: "spacer", style: "flex:1" }),
      statusBadge(t.status));
    const body = [];
    if (!isRoot && t.objective && t.title && t.objective !== t.title) {
      body.push(el("div", { class: "tn-obj" }, t.objective));
    }
    if (deps.length) {
      const chips = el("div", { class: "dep-chips" }, el("span", {}, "依赖："));
      for (const depId of deps) {
        const dep = byId.get(String(depId));
        chips.appendChild(el("span", { class: "badge" }, dep ? (dep.title || dep.objective || String(depId)).slice(0, 18) : String(depId).slice(0, 10)));
      }
      body.push(chips);
    }
    const miniResult = miniResultOf(t);
    if (miniResult) body.push(el("div", { class: "tn-result" }, miniResult));

    const wrap = el("div", { class: "tree-node" }, head, body.length ? el("div", { class: "tn-body" }, body) : null);

    const kids = childrenOf.get(String(t.id)) || [];
    if (kids.length) {
      const childWrap = el("div", { class: "tree-children" });
      for (const k of kids) childWrap.appendChild(nodeEl(k, false));
      return el("div", {}, wrap, childWrap);
    }
    return el("div", {}, wrap);
  }

  return el("div", { class: "tree" }, nodeEl(d.root, true));
}

function miniResultOf(t) {
  const r = t.result;
  if (!r || (typeof r === "object" && !Object.keys(r).length)) return null;
  const parts = [];
  if (r.answer) parts.push(el("span", {}, el("span", { class: "lab" }, "结论："), String(r.answer).slice(0, 120)));
  if (r.geo && Array.isArray(r.geo.candidates) && r.geo.candidates.length) {
    parts.push(el("span", {}, el("span", { class: "lab" }, "地点："), `找到 ${r.geo.candidates.length} 个候选`));
  }
  if (typeof r.verified === "boolean") {
    parts.push(el("span", {}, r.verified ? badge("核验通过", "ok") : badge("核验未通过", "danger")));
  }
  if (!parts.length) return null;
  return el("div", { class: "row-gap" }, ...parts);
}

// ---------- 结果区 ----------
function renderResult(app, d) {
  const r = d.result || {};
  const hasContent = r.answer || r.verified !== undefined || (Array.isArray(r.chunks) && r.chunks.length) ||
    r.fail_reason || r.failure_reason || r.handoff || (Array.isArray(r.unknowns) && r.unknowns.length) ||
    r.note || r.summary;
  if (!hasContent && TERMINAL_STATUS.has(d.root.status) === false) return;

  const card = el("div", { class: "card" },
    el("h2", {}, "结果",
      el("span", { class: "hint" }, "事实性结论都带有可核查的引用角标")));

  // 核验徽标
  if (typeof r.verified === "boolean") {
    card.appendChild(el("div", { class: "row-gap", style: "margin-bottom:10px" },
      r.verified ? badge("核验通过", "ok") : badge("核验未通过", "danger"),
      r.verified_note || r.verify_note ? el("span", { class: "muted", style: "font-size:13px" }, r.verified_note || r.verify_note) : null));
  }

  if (r.answer) card.appendChild(buildAnswer(r.answer, collectChunks(d), card));
  else if (hasContent) card.appendChild(el("div", { class: "muted" }, r.summary || r.note || "任务尚未产出结论。"));

  // 未通过原因
  const failReason = r.fail_reason || r.failure_reason || r.unverified_reason || (r.verified === false ? r.reason : null);
  if (failReason) {
    card.appendChild(el("div", { class: "note-box danger" }, el("span", { class: "lab" }, "未通过原因："), String(failReason)));
  }
  if (Array.isArray(r.conflicts) && r.conflicts.length) {
    card.appendChild(el("div", { class: "note-box warn" },
      el("span", { class: "lab" }, "来源冲突："),
      r.conflicts.map((c) => typeof c === "string" ? c : JSON.stringify(c)).join("；")));
  }

  // unknown 标注
  const unknowns = asArray(r.unknowns || r.unknown);
  if (unknowns.length) {
    card.appendChild(el("div", { class: "note-box info" },
      el("span", { class: "lab" }, "无法确认（如实标注）："),
      el("ul", { class: "unknown-list" }, unknowns.map((u) => el("li", {}, typeof u === "string" ? u : (u.text || u.item || JSON.stringify(u)))))));
  }

  // 人工交接
  const handoff = r.handoff || r.handover;
  if (handoff) {
    const text = typeof handoff === "string" ? handoff : (handoff.reason || handoff.message || handoff.note || JSON.stringify(handoff));
    const channel = typeof handoff === "object" ? (handoff.channel || handoff.via) : null;
    card.appendChild(el("div", { class: "note-box warn" },
      el("span", { class: "lab" }, "需要人工交接："),
      text,
      channel ? el("div", { style: "margin-top:4px" }, `建议渠道：${channel}`) : null));
  }

  // 证据
  const chunks = collectChunks(d);
  if (chunks.length) {
    const list = el("div", { class: "evidence-list" });
    chunks.forEach((c, i) => {
      const num = i + 1;
      const cites = asArray(c.citations || c.cites);
      const fields = c.fields && typeof c.fields === "object" ? Object.entries(c.fields) : [];
      list.appendChild(el("div", { class: "evidence", id: `ev-${num}` },
        el("div", { class: "ev-head" },
          el("span", { class: "ev-num" }, `[${num}]`),
          el("span", { class: "ev-doc" }, c.doc_name || c.doc || "未命名文档"),
          c.section ? el("span", { class: "ev-sec" }, "· " + c.section) : null),
        el("div", { class: "ev-text" }, c.text || ""),
        cites.length ? el("div", { class: "ev-cites" },
          cites.map((x) => el("span", { class: "cite-tag" }, typeof x === "string" ? x : JSON.stringify(x)))) : null,
        fields.length ? el("div", { class: "ev-cites" },
          fields.map(([k, v]) => el("span", { class: "cite-tag" }, `${k}: ${v}`))) : null,
        (c.updated_at || c.source_time || c.access_time)
          ? el("div", { class: "ev-time" }, "来源时间：" + fmtTime(c.updated_at || c.source_time || c.access_time))
          : null));
    });
    card.appendChild(el("div", { style: "margin-top:14px" },
      el("div", { class: "muted", style: "font-size:12.5px;margin-bottom:6px" }, "证据来源（点击答案中的角标可定位）"),
      list));
  }

  app.appendChild(card);
}

function collectChunks(d) {
  const map = new Map();
  const pushList = (list) => {
    for (const c of asArray(list)) {
      const id = String(c.chunk_id || c.id || `auto-${map.size}`);
      if (!map.has(id)) map.set(id, c);
    }
  };
  pushList(d.result.chunks);
  pushList(d.result.evidence);
  for (const t of d.subtasks) {
    const r = t.result || {};
    pushList(r.chunks);
    pushList(r.evidence);
  }
  return [...map.values()];
}

function buildAnswer(answer, chunks, card) {
  const wrap = el("div", { class: "answer-text" });
  const idToNum = new Map();
  chunks.forEach((c, i) => {
    if (c.chunk_id) idToNum.set(String(c.chunk_id), i + 1);
    if (c.id) idToNum.set(String(c.id), i + 1);
  });
  // 匹配 [cite:xxx]、[1]、【1】 等引用写法
  const re = /\[cite:([^\]\s]+)\]|\[(\d{1,3})\]/g;
  let last = 0;
  let m;
  const makeRef = (label, targetNum, known) => el("button", {
    class: `cite-ref${known ? "" : " unknown"}`,
    title: known ? `跳转到证据 [${targetNum}]` : "未找到对应证据",
    onClick: () => {
      if (!known) return;
      const ev = card.querySelector(`#ev-${targetNum}`);
      if (ev) {
        ev.scrollIntoView({ behavior: "smooth", block: "center" });
        ev.classList.remove("flash");
        void ev.offsetWidth;
        ev.classList.add("flash");
      }
    },
  }, label);

  while ((m = re.exec(answer)) !== null) {
    wrap.appendChild(document.createTextNode(answer.slice(last, m.index)));
    if (m[1] !== undefined) {
      // [cite:chunk_id]
      const num = idToNum.get(String(m[1]));
      wrap.appendChild(makeRef(num ? String(num) : "?", num, !!num));
    } else {
      // [n]
      const n = parseInt(m[2], 10);
      const known = n >= 1 && n <= chunks.length;
      wrap.appendChild(makeRef(m[2], n, known));
    }
    last = re.lastIndex;
  }
  wrap.appendChild(document.createTextNode(answer.slice(last)));
  return wrap;
}

// ---------- GEO ----------
function collectGeo(d) {
  const out = [];
  const consider = (r, title, key) => {
    const g = r && r.geo;
    if (g && Array.isArray(g.candidates) && g.candidates.length) {
      out.push({ geo: g, title: title || "地理分析", key });
    }
  };
  consider(d.result, "地点分析图", "result");
  d.subtasks.forEach((t, i) => consider(t.result,
    `地点分析（${roleLabel(t.agent_role)}：${(t.title || t.objective || "").slice(0, 20)}）`, `st${i}`));
  return out;
}

const GEO_COLORS = ["#4f6ef7", "#1a9e6b", "#d2732e", "#8459d6", "#c7458a", "#2b7fb8", "#b58a1f"];

// ---- 地图卡片：[地图] [地点] 选项卡；地图由当前地图插件渲染，无可用插件回退内联 SVG ----
function buildGeoCard({ geo, title, key }, geoKey) {
  const card = el("div", { class: "card geo-card" });
  card.appendChild(el("h2", {}, title || "地理分析"));

  const source = geo.source || geo.data_source || "provider";
  const badgeRow = el("div", { class: "row-gap", style: "margin:6px 0 10px" },
    source === "local_geojson" ? badge("本地样例数据", "info")
      : source === "mock" ? badge("模拟数据", "warn")
      : badge(`真实数据 · ${source}`, "ok"),
    geo.radius_km != null ? el("span", { class: "badge" }, `半径 ${geo.radius_km} km`) : null,
    geo.crs ? el("span", { class: "mono muted" }, geo.crs) : null);
  card.appendChild(badgeRow);

  // 选项卡
  const tabBar = el("div", { class: "geo-tabs", role: "tablist" },
    el("button", { type: "button", class: "geo-tab active", "data-tab": "map" }, "地图"),
    el("button", { type: "button", class: "geo-tab", "data-tab": "list" }, `地点（${asArray(geo.candidates).length}）`));
  card.appendChild(tabBar);

  const mapPane = el("div", { class: "geo-pane", "data-pane": "map" });
  const listPane = el("div", { class: "geo-pane", "data-pane": "list", hidden: true });
  card.appendChild(mapPane);
  card.appendChild(listPane);

  tabBar.querySelectorAll(".geo-tab").forEach((btn) => btn.addEventListener("click", () => {
    tabBar.querySelectorAll(".geo-tab").forEach((b) => b.classList.toggle("active", b === btn));
    const tab = btn.dataset.tab;
    mapPane.hidden = tab !== "map";
    listPane.hidden = tab !== "list";
  }));

  const panelKey = geoKey + ":" + (key || "x");
  buildMapPane(mapPane, geo, panelKey);
  buildSiteList(listPane, card, geo, panelKey);
  return card;
}

// ---- 地图控制器（通用 MapPanel，与具体地图 Provider 解耦；实现见 maps/panel.js）----
const mapController = createMapController({
  el, toast, asArray, categoryOf, buildGeoSvg,
  getConfig: () => state.config || {},
});

function buildMapPane(pane, geo, geoKey) { mapController.buildMapPane(pane, geo, geoKey); }
function buildSiteList(pane, cardEl, geo, geoKey) { mapController.buildSiteList(pane, cardEl, geo, geoKey); }

function buildGeoSvg(geo) {
  const W = 720, H = 440, PAD = 46;
  const center = geo.center || null;
  const cands = asArray(geo.candidates).filter((c) => Number.isFinite(+c.lat) && Number.isFinite(+c.lon));

  let minLat = Infinity, maxLat = -Infinity, minLon = Infinity, maxLon = -Infinity;
  const consider = (lat, lon) => {
    minLat = Math.min(minLat, lat); maxLat = Math.max(maxLat, lat);
    minLon = Math.min(minLon, lon); maxLon = Math.max(maxLon, lon);
  };
  if (center && Number.isFinite(+center.lat) && Number.isFinite(+center.lon)) consider(+center.lat, +center.lon);
  for (const c of cands) consider(+c.lat, +c.lon);

  // 半径圆范围（按 km→度换算：1° lat ≈ 111km，经度按 cos(lat) 修正）
  let radiusDegLat = 0, radiusDegLon = 0;
  if (center && geo.radius_km != null) {
    const lat0 = +center.lat;
    radiusDegLat = (+geo.radius_km) / 111;
    radiusDegLon = (+geo.radius_km) / (111 * Math.cos(lat0 * Math.PI / 180));
    consider(lat0 + radiusDegLat, (+center.lon) - radiusDegLon);
    consider(lat0 - radiusDegLat, (+center.lon) + radiusDegLon);
  }
  if (!isFinite(minLat)) { minLat = 0; maxLat = 1; minLon = 0; maxLon = 1; }
  // 边距
  const padLat = Math.max((maxLat - minLat) * 0.15, 0.0005);
  const padLon = Math.max((maxLon - minLon) * 0.15, 0.0005);
  minLat -= padLat; maxLat += padLat; minLon -= padLon; maxLon += padLon;

  const spanLat = Math.max(maxLat - minLat, 1e-9);
  const spanLon = Math.max(maxLon - minLon, 1e-9);
  // 保持视觉比例：经度跨度按 cos(lat) 折算
  const midLat = (minLat + maxLat) / 2;
  const xScale = (W - PAD * 2) / (spanLon * Math.cos(midLat * Math.PI / 180) * 111 || 1);
  const yScale = (H - PAD * 2) / (spanLat * 111 || 1);
  const scale = Math.min(xScale, yScale);

  const proj = (lat, lon) => ({
    x: PAD + ((+lon) - minLon) * Math.cos(midLat * Math.PI / 180) * 111 * scale,
    y: H - PAD - ((+lat) - minLat) * 111 * scale,
  });

  const svg = elNS("svg", {
    viewBox: `0 0 ${W} ${H}`, class: "geo-map", role: "img",
    "aria-label": "GEO 候选地点地图",
  });

  // 半径圆（在地面是圆，投影后为椭圆）
  if (center && geo.radius_km != null && Number.isFinite(+center.lat)) {
    const c = proj(+center.lat, +center.lon);
    const rx = radiusDegLon * Math.cos(midLat * Math.PI / 180) * 111 * scale;
    const ry = radiusDegLat * 111 * scale;
    svg.appendChild(elNS("ellipse", {
      cx: c.x, cy: c.y, rx, ry,
      fill: "var(--accent)", "fill-opacity": ".07",
      stroke: "var(--text-3)", "stroke-width": 1.5, "stroke-dasharray": "6 5",
    }));
    svg.appendChild(elNS("text", { x: c.x + rx * 0.7, y: c.y - ry * 0.7, "font-size": 11, fill: "var(--text-3)" }, `${geo.radius_km} km`));
  }

  // 中心点
  if (center && Number.isFinite(+center.lat)) {
    const c = proj(+center.lat, +center.lon);
    svg.appendChild(elNS("rect", {
      x: c.x - 6, y: c.y - 6, width: 12, height: 12, rx: 2,
      fill: "var(--danger)", stroke: "var(--bg-elev)", "stroke-width": 2,
    }));
    const label = center.name || "中心";
    svg.appendChild(elNS("text", { x: c.x + 10, y: c.y - 8, "font-size": 12, "font-weight": 700, fill: "var(--text)" }, label));
  }

  // 候选点着色（按类别）
  const categories = [...new Set(cands.map((c) => categoryOf(c)))];
  const colorOf = new Map(categories.sort().map((cat, i) => [cat, GEO_COLORS[i % GEO_COLORS.length]]));

  // 重渲染时清理上一张地图遗留的悬浮提示
  document.querySelectorAll(".geo-tip").forEach((t) => t.remove());
  const tip = document.createElement("div");
  tip.className = "geo-tip";
  document.body.appendChild(tip);

  cands.forEach((c) => {
    const p = proj(+c.lat, +c.lon);
    const color = colorOf.get(categoryOf(c));
    const g = elNS("g", { class: "geo-candidate" });
    g.appendChild(elNS("circle", { class: "pt", cx: p.x, cy: p.y, r: 5.5, fill: color, stroke: "var(--bg-elev)", "stroke-width": 2 }));
    g.appendChild(elNS("circle", { cx: p.x, cy: p.y, r: 11, fill: color, "fill-opacity": 0 }));
    g.appendChild(elNS("text", { x: p.x + 9, y: p.y + 4, "font-size": 11.5, fill: "var(--text-2)" }, c.name || "候选点"));

    const showTip = (ev) => {
      tip.innerHTML = "";
      tip.appendChild(el("div", { class: "gt-name" }, c.name || "候选点"));
      if (c.distance_km != null) tip.appendChild(el("div", { class: "gt-row" }, `距离中心：${(+c.distance_km).toFixed(2)} km`));
      if (categoryOf(c)) tip.appendChild(el("div", { class: "gt-row" }, `类别：${categoryOf(c)}`));
      const props = c.properties || c.props;
      if (props && typeof props === "object") {
        for (const [k, v] of Object.entries(props)) {
          if (v == null) continue;
          tip.appendChild(el("div", { class: "gt-row" }, `${k}：${v}`));
        }
      }
      if (c.evidence) tip.appendChild(el("div", { class: "gt-ev" }, typeof c.evidence === "string" ? c.evidence : JSON.stringify(c.evidence)));
      if (c.source) tip.appendChild(el("div", { class: "gt-ev muted" }, `来源：${c.source}`));
      tip.classList.add("show");
      moveTip(ev);
      g.classList.add("active");
    };
    const moveTip = (ev) => {
      const tW = tip.offsetWidth || 280;
      let x = ev.clientX + 14;
      let y = ev.clientY + 14;
      if (x + tW > window.innerWidth - 8) x = ev.clientX - tW - 14;
      tip.style.left = x + "px";
      tip.style.top = y + "px";
    };
    const hideTip = () => { tip.classList.remove("show"); g.classList.remove("active"); };
    g.addEventListener("mouseenter", showTip);
    g.addEventListener("mousemove", moveTip);
    g.addEventListener("mouseleave", hideTip);
    g.addEventListener("click", showTip);
    svg.appendChild(g);
  });

  // 移除 tooltip：在 svg 被移除时（innerHTML 清空）tip 节点不会自动消失，用 observer 不必要——
  // 简单处理：鼠标离开 svg 隐藏。
  svg.addEventListener("mouseleave", () => tip.classList.remove("show"));

  return { svg, legendCategories: [...colorOf.entries()] };
}

function categoryOf(c) {
  return c.category || c.type || (c.properties && (c.properties.category || c.properties.type)) || "";
}

function elNS(name, attrs = {}, ...children) {
  const node = document.createElementNS("http://www.w3.org/2000/svg", name);
  for (const [k, v] of Object.entries(attrs)) {
    if (v == null || v === false) continue;
    node.setAttribute(k, v);
  }
  for (const c of children.flat()) {
    if (c == null) continue;
    node.appendChild(typeof c === "string" || typeof c === "number" ? document.createTextNode(c) : c);
  }
  return node;
}

// ---------- 事件时间线 ----------
function buildTimeline(events) {
  const sorted = [...events].sort((a, b) => {
    const ta = a.ts || a.created_at || "";
    const tb = b.ts || b.created_at || "";
    return String(ta).localeCompare(String(tb)) || ((+a.id || 0) - (+b.id || 0));
  });
  const wrap = el("div", { class: "timeline" });
  if (!sorted.length) {
    wrap.appendChild(el("p", { class: "muted" }, "暂无动作记录。"));
    return wrap;
  }
  for (const e of sorted) {
    const kind = e.kind || "unknown";
    const item = el("div", { class: `tl-item kind-${kind}` },
      el("div", { class: "tl-head" },
        el("span", { class: "tl-time" }, fmtTime(e.ts || e.created_at)),
        badge(EVENT_LABELS[kind] || kind, eventBadgeKind(kind)),
        e.actor ? el("span", { class: "muted", style: "font-size:12px" }, e.actor) : null,
        el("span", { class: "tl-summary" }, eventSummary(kind, e.detail))),
      detailDisclosure(e.detail));
    wrap.appendChild(item);
  }
  return wrap;
}

function eventBadgeKind(kind) {
  if (kind === "error" || kind === "tool_denied") return "danger";
  if (kind === "tool_result") return "ok";
  if (kind === "approval_requested" || kind === "approval_decided") return "warn";
  if (kind === "gate_entry" || kind === "gate_exit") return "purple";
  if (kind === "recovery") return "info";
  if (kind === "task_status" || kind === "task_created") return "accent";
  return "";
}

function eventSummary(kind, d) {
  d = d || {};
  switch (kind) {
    case "tool_request":
    case "tool_result":
    case "tool_denied":
      return [d.tool_name || d.tool, d.status ? `（${d.status}）` : "", d.error ? `错误：${d.error}` : ""].filter(Boolean).join(" ");
    case "approval_requested":
      return `${d.tool_name || ""} 等待审批${d.reason ? "：" + d.reason : ""}`;
    case "approval_decided":
      return `${d.tool_name || ""} → ${d.decision === "approved" ? "批准" : d.decision === "rejected" ? "拒绝" : (d.decision || "")}${d.decided_by ? "（" + d.decided_by + "）" : ""}`;
    case "task_status":
      return `${STATUS_LABELS[d.from] || d.from || "?"} → ${STATUS_LABELS[d.to] || d.to || "?"}${d.reason ? "：" + d.reason : ""}`;
    case "task_created":
      return d.title || d.objective || "";
    case "error":
      return d.message || d.error || d.reason || "";
    case "recovery":
      return d.reason || d.message || "恢复执行";
    case "gate_entry":
      return d.passed === false ? "入口闸门未通过" : "进入入口闸门";
    case "gate_exit":
      return d.passed === false ? `出口闸门未通过${d.reason ? "：" + d.reason : ""}` : "出口闸门核验通过";
    case "message":
      return d.text || d.message || "";
    case "llm_call":
      return d.driver ? `驱动：${d.driver}` : "";
    default:
      return d.message || d.reason || "";
  }
}

function detailDisclosure(detail) {
  if (!detail || (typeof detail === "object" && !Object.keys(detail).length)) return null;
  const text = JSON.stringify(detail, null, 2);
  if (text === "{}") return null;
  return el("details", { class: "tl-detail" },
    el("summary", {}, "详情"),
    el("pre", {}, text));
}

// ============================== 插件区 ==============================
async function renderPlugins(app) {
  app.appendChild(el("p", { class: "loading" }, "加载插件中…"));
  let list = [];
  try {
    const data = await api("/api/plugins");
    list = normalizePlugins(data);
  } catch (err) {
    app.innerHTML = "";
    app.appendChild(el("div", { class: "empty" },
      el("div", { class: "big" }, "插件信息加载失败"),
      el("div", {}, errorHint(err)),
      el("div", { style: "margin-top:12px" }, el("button", { class: "btn", onClick: () => render() }, "重试"))));
    return;
  }
  app.innerHTML = "";
  app.appendChild(el("div", { class: "page-head" },
    el("h1", {}, "插件"),
    el("span", { class: "sub" }, "插件给系统增加工具和知识；声明的权限不会自动获得授权"),
    el("span", { class: "spacer" }),
    el("button", { class: "btn sm", onClick: () => render() }, "刷新")));

  // 地图插件分区（工作台展示/路线/跳转用；与工具插件区分）
  app.appendChild(buildMapPluginsSection());

  if (!list.length) {
    app.appendChild(el("div", { class: "empty" }, el("div", { class: "big" }, "没有已加载的工具插件")));
    return;
  }

  const wrap = el("div", { class: "plugin-grid" });
  for (const p of list) {
    const enabled = p.enabled !== false && !p.error;
    const card = el("div", { class: `plugin-card${enabled ? "" : " disabled"}` },
      el("div", { class: "pc-head" },
        el("span", { class: "pc-name" }, p.name),
        p.version ? el("span", { class: "mono muted" }, "v" + p.version) : null,
        el("span", { class: "spacer", style: "flex:1" }),
        enabled ? badge("已启用", "ok") : badge(p.error ? "加载失败" : "已禁用", p.error ? "danger" : "")));
    if (p.description) card.appendChild(el("div", { class: "pc-desc" }, p.description));
    if (asArray(p.capabilities || p.tools).length) {
      card.appendChild(el("div", { class: "pc-row" },
        el("span", { class: "lab" }, "能力 / 工具："),
        asArray(p.capabilities || p.tools).map((t) => typeof t === "string" ? t : (t.name || JSON.stringify(t))).join("、")));
    }
    if (asArray(p.permissions).length) {
      card.appendChild(el("div", { class: "pc-row" },
        el("span", { class: "lab" }, "声明权限："),
        asArray(p.permissions).map((perm) => el("span", { class: "perm-tag" }, String(perm)))));
    }
    if (p.error || p.load_error) {
      card.appendChild(el("div", { class: "plugin-error" }, "加载错误：\n" + String(p.error || p.load_error)));
    }
    wrap.appendChild(card);
  }
  app.appendChild(wrap);
}


// ---- 插件页：地图插件分区（由注册表通用渲染，不写死任何 Provider）----
function buildMapPluginsSection() {
  const mapCfg = (state.config && state.config.map_plugins) || {};
  const section = el("section", { class: "map-plugin-section" });
  section.appendChild(el("h2", { style: "margin-bottom:4px" }, "地图插件",
    el("span", { class: "hint" }, "负责真实地图展示、道路路线与导航跳转；当前启用哪个就用哪个，Agent 地理分析不依赖地图")));

  const wrap = el("div", { class: "plugin-grid" });
  const activeId = getActiveMapPluginId(mapCfg);
  const manual = getManualMapPlugin();
  const items = listMapPlugins(mapCfg);

  for (const m of items) {
    const isActive = m.available && m.id === activeId && !manual ? true
      : (m.available && m.id === activeId);
    const card = el("div", { class: `plugin-card${isActive ? "" : " disabled"}` },
      el("div", { class: "pc-head" },
        el("span", { class: "pc-name" }, m.name),
        m.version ? el("span", { class: "mono muted" }, "v" + m.version) : null,
        el("span", { class: "spacer", style: "flex:1" }),
        m.coming_soon ? badge("Coming Soon", "")
          : isActive ? badge("当前使用", "ok")
          : m.available ? badge("可用", "info")
          : badge("未配置", "")));

    // 能力
    const caps = m.capabilities || {};
    const capBits = [];
    if (caps.map) capBits.push("地图");
    if (caps.markers) capBits.push("地点标记");
    if (caps.circle) capBits.push("半径范围");
    if (caps.route) capBits.push("路线规划");
    if (caps.externalSite || caps.externalRoute) capBits.push("外部跳转");
    if (capBits.length) card.appendChild(el("div", { class: "pc-row" }, el("span", { class: "lab" }, "能力："), capBits.join("、")));
    if (Array.isArray(caps.routeModes) && caps.routeModes.length) {
      const labels = { driving: "驾车", walking: "步行", transit: "公交", cycling: "骑行" };
      card.appendChild(el("div", { class: "pc-row muted" },
        "路线方式：" + caps.routeModes.map((x) => labels[x] || x).join("、")));
    }
    // 配置状态（通用描述，不写死字段）
    const cs = m.configStatus || {};
    if (m.id === "amap" && !m.coming_soon) {
      card.appendChild(el("div", { class: "pc-row muted" },
        cs.jsKeyConfigured ? "JS Key 已配置。" : "未配置 JS Key：地理分析将显示离线示意图（.env 设置 AMAP_JS_KEY 后重启生效）。"));
      if (cs.city) card.appendChild(el("div", { class: "pc-row muted" }, "公交默认城市：" + cs.city));
    }

    const btns = el("div", { style: "margin-top:10px" });
    if (m.coming_soon || !m.available) {
      btns.appendChild(el("span", { class: "muted" }, m.coming_soon ? "尚未配置，未来可在此切换。" : "配置所需 Key 后即可启用。"));
    } else {
      btns.appendChild(el("button", {
        class: `btn sm${isActive ? "" : " ghost"}`,
        disabled: isActive && !manual,
        onClick: () => { setActiveMapPlugin(m.id); toast(`已切换到 ${m.name}，回到任务详情即可看到。`, "success"); render(); },
      }, isActive ? "当前使用" : "切换到此地图"));
      if (manual) {
        btns.appendChild(el("button", {
          class: "btn sm ghost", style: "margin-left:8px",
          onClick: () => { setActiveMapPlugin(null); toast("已恢复系统默认地图。", "info"); render(); },
        }, "恢复默认"));
      }
    }
    card.appendChild(btns);
    wrap.appendChild(card);
  }
  section.appendChild(wrap);
  return section;
}

function normalizePlugins(data) {
  if (Array.isArray(data)) return data;
  if (data && Array.isArray(data.plugins)) return data.plugins;
  if (data && typeof data === "object") return Object.entries(data).map(([name, v]) => ({ name, ...(v || {}) }));
  return [];
}

// ============================== 评测区 ==============================
async function renderEval(app) {
  app.appendChild(el("div", { class: "page-head" },
    el("h1", {}, "回归评测"),
    el("span", { class: "sub" }, "真实运行内置用例；只展示实际结果，不造数"),
    el("span", { class: "spacer" }),
    el("button", {
      class: "btn primary", id: "eval-run",
      onClick: async (ev) => {
        const btn = ev.target;
        btn.disabled = true;
        btn.textContent = "评测运行中…（可能需要一点时间）";
        try {
          const res = await api("/api/eval/run", { method: "POST" });
          renderEvalWith(app, normalizeEval(res));
        } catch (err) {
          toast(errorHint(err), "error", "评测运行失败");
          btn.disabled = false;
          btn.textContent = "运行评测";
        }
      },
    }, "运行评测")));

  const holder = el("div", { id: "eval-holder" });
  holder.appendChild(el("div", { class: "empty" },
    el("div", { class: "big" }, "尚未运行"),
    el("div", {}, "点击「运行评测」执行全部回归用例（会在临时环境中跑完整任务流程）。")));
  app.appendChild(holder);
}

function renderEvalWith(app, results) {
  const holder = $("#eval-holder");
  if (!holder) return;
  holder.innerHTML = "";
  const cases = results.cases || results;
  const total = cases.length;
  const passed = cases.filter((c) => c.passed === true || c.passed === 1).length;
  const rate = total ? Math.round((passed / total) * 100) : 0;

  holder.appendChild(el("div", { class: "eval-summary card" },
    el("div", { class: "eval-rate", style: `color:${rate === 100 ? "var(--ok)" : rate >= 60 ? "var(--warn)" : "var(--danger)"}` }, `${rate}%`),
    el("div", {},
      el("div", { style: "font-weight:700" }, `通过 ${passed} / ${total} 条用例`),
      el("div", { class: "muted", style: "font-size:12.5px" }, results.ts ? `运行时间：${fmtTime(results.ts)}` : ""))));

  for (const c of cases) {
    const ok = c.passed === true || c.passed === 1;
    const checks = normalizeChecks(c.checks);
    const caseEl = el("div", { class: "eval-case" },
      el("div", { class: "ec-head" },
        ok ? badge("通过", "ok") : badge("失败", "danger"),
        el("span", { class: "ec-name" }, c.name || c.case_id || c.id || "未命名用例"),
        c.scenario ? el("span", { class: "badge" }, String(c.scenario)) : null));
    if (checks.length) {
      caseEl.appendChild(el("ul", { class: "check-list" },
        checks.map((ck) => el("li", { class: ck.pass ? "pass" : "fail" },
          el("span", { class: "ck-icon" }, ck.pass ? "✓" : "✗"),
          el("span", {}, ck.name || ck.check || JSON.stringify(ck))))));
    }
    if (c.note) caseEl.appendChild(el("div", { class: "eval-note" }, String(c.note)));
    if (c.error) caseEl.appendChild(el("div", { class: "eval-note", style: "color:var(--danger)" }, String(c.error)));
    holder.appendChild(caseEl);
  }
}

function normalizeEval(data) {
  if (!data) return { cases: [] };
  if (Array.isArray(data)) return { cases: data };
  if (Array.isArray(data.results)) return { ts: data.ts, cases: data.results };
  if (Array.isArray(data.cases)) return { ts: data.ts, cases: data.cases };
  if (Array.isArray(data.runs)) return { ts: data.ts, cases: data.runs };
  return { ts: data.ts, cases: [] };
}

function normalizeChecks(checks) {
  if (!checks) return [];
  if (Array.isArray(checks)) {
    return checks.map((c) => {
      if (typeof c === "string") return { name: c, pass: true };
      return { name: c.name || c.check || c.id, pass: c.pass ?? c.passed ?? c.ok ?? true, ...c };
    });
  }
  if (typeof checks === "object") {
    return Object.entries(checks).map(([k, v]) => ({
      name: k,
      pass: typeof v === "object" ? (v.pass ?? v.passed ?? true) : !!v,
    }));
  }
  return [];
}

// ============================== 轮询 ==============================
function startPolling() {
  if (state.pollTimer) return;
  state.pollTimer = setInterval(() => {
    if (document.visibilityState !== "visible") return;
    if (state.view !== "detail") { stopPolling(); return; }
    if (state.pollInFlight) return;
    state.pollInFlight = true;
    renderDetail($("#app"), true).finally(() => { state.pollInFlight = false; });
  }, 2000);
}

function stopPolling() {
  if (state.pollTimer) {
    clearInterval(state.pollTimer);
    state.pollTimer = null;
  }
}

// ============================== 启动 ==============================
function init() {
  document.querySelectorAll(".tab").forEach((t) => {
    t.addEventListener("click", () => setView(t.dataset.view));
  });
  $("#brand").addEventListener("click", () => setView("tasks"));

  checkHealth();
  state.healthTimer = setInterval(checkHealth, 10000);
  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState === "visible" && state.view === "detail") renderDetail($("#app"), true);
  });

  setView("tasks");
}

init();
