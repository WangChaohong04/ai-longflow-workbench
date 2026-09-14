// Uses the workbench's authenticated API client and safe DOM builder.
export async function showCapabilities(host, { api, el }) {
  host.textContent = "正在检查配置…";
  try {
    const data = await api("/api/capabilities");
    host.replaceChildren(el("h2", {}, "能力状态"),
      el("p", { class: "muted" }, "配置齐全不代表真实服务已验收。当前任务规划使用本地规则。"));
    for (const c of data.capabilities || []) host.appendChild(el("div", { class: "note-box", "data-capability": c.key },
      el("strong", {}, `${c.label}：${c.ready ? "可调用" : "未就绪"}`), el("div", {}, c.desc)));
  } catch (e) {
    host.replaceChildren(el("p", { role: "alert" }, `加载失败：${e.message}`),
      el("button", { class: "btn", onClick: () => showCapabilities(host, { api, el }) }, "重新检查"));
  }
}

export async function showKnowledgePreview(host, id, { api, el }) {
  host.textContent = "加载文档正文…";
  try {
    const doc = await api(`/api/knowledge/docs/${encodeURIComponent(id)}`);
    host.replaceChildren(el("h2", {}, `预览：${doc.title}`),
      el("p", { class: "muted" }, `版本：${doc.version || "未标注"} · ${doc.section_count} 段`));
    for (const warning of doc.warnings || []) host.appendChild(el("p", { role: "alert" }, warning));
    for (const s of doc.preview || []) host.appendChild(el("details", { open: true, class: "note-box" },
      el("summary", {}, `${s.section || "正文"}${s.page ? ` · 第 ${s.page} 页` : ""}`),
      el("div", { style: "white-space:pre-wrap", "data-doc-section": "" }, s.text)));
    const input = el("input", { placeholder: "输入试检索问题", "aria-label": "试检索问题" });
    const result = el("div", { "data-trial-result": "", role: "status" });
    const button = el("button", { type: "submit", class: "btn" }, "试检索（不激活）");
    host.appendChild(el("form", { onSubmit: async e => {
      e.preventDefault();
      if (!input.value.trim()) { result.textContent = "请先输入问题。"; return; }
      button.disabled = true; result.textContent = "检索中…";
      try {
        const data = await api(`/api/knowledge/docs/${encodeURIComponent(id)}/trial`, {
          method: "POST", body: JSON.stringify({ query: input.value.trim() }) });
        result.replaceChildren(el("p", {}, `命中 ${data.hit_count} 段；文档激活状态未改变。`));
        for (const hit of data.hits || []) result.appendChild(el("div", { class: "note-box" },
          el("strong", {}, `${hit.citation?.doc || ""} · ${hit.section || "正文"} · 版本 ${hit.citation?.version || "未标注"}`),
          el("p", {}, hit.excerpt)));
      } catch (err) { result.textContent = `试检索失败：${err.message}`; }
      finally { button.disabled = false; }
    } }, input, button, result));
  } catch (err) { host.textContent = `无法预览：${err.message}`; }
}

export function resultActions(d, { api, el, toast, refresh }) {
  const host = el("div", { class: "card", "data-result-actions": "" });
  const row = el("div", { class: "row-gap" });
  for (const format of ["md", "csv"]) row.appendChild(el("button", { class: "btn sm", onClick: async e => {
    const button = e.currentTarget;
    button.disabled = true;
    try {
      const text = await api(`/api/tasks/${encodeURIComponent(d.root.id)}/export?format=${format}`, { responseFormat: "text" });
      const url = URL.createObjectURL(new Blob([text], { type: format === "csv" ? "text/csv;charset=utf-8" : "text/markdown;charset=utf-8" }));
      const a = el("a", { href: url, download: `longflow-${d.root.id}.${format}` });
      document.body.appendChild(a); a.click(); a.remove();
      setTimeout(() => URL.revokeObjectURL(url), 1000);
      toast("报告已导出。", "success");
    } catch (err) { toast(`导出失败：${err.message}`, "error"); }
    finally { button.disabled = false; }
  } }, format === "md" ? "导出 Markdown" : "导出 CSV"));
  host.appendChild(row);
  const groups = new Map();
  const failed = d.subtasks.filter(t => t.status === "failed" && t.plan?.engine === "coordinator" && t.plan?.node_kind === "research");
  for (const task of failed) {
    const key = task.plan.branch || task.id;
    if (!groups.has(key)) groups.set(key, []);
    groups.get(key).push(task);
  }
  for (const [key, tasks] of groups) {
    const button = el("button", { class: "btn sm", onClick: async () => {
      button.disabled = true; button.textContent = "提交重试…";
      try {
        const data = await api(`/api/tasks/${encodeURIComponent(d.root.id)}/retry-branch`, {
          method: "POST", body: JSON.stringify({ branch: key }) });
        if (data.error || !data.retried?.length) throw new Error(data.error || "当前没有可重试的失败节点");
        toast("失败分支已重新排队，已完成证据继续保留。", "success");
        await refresh();
      } catch (err) { toast(`重试失败：${err.message}`, "error"); button.disabled = false; button.textContent = "重试此分支"; }
    } }, "重试此分支");
    host.appendChild(el("div", { class: "note-box warn", "data-failed-branch": key },
      el("strong", {}, tasks[0].plan.branch || tasks[0].title),
      ...tasks.map(t => el("div", {}, `${t.title}：${t.result?.error || "来源失败"}`)), button));
  }
  return host;
}
