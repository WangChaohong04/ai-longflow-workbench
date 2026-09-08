/* MapPanel 控制器 —— 地理分析卡片的地图/地点/路线 UI 编排。
 *
 * 只与地图插件注册表 + 统一插件接口交互，不含任何 Provider 专属逻辑。
 * 通过 deps 注入 DOM 助手，便于在 node 中用 stub 做真实调用路径测试。
 *
 * 关键不变式：
 *  - 同一 geoKey、同一 active plugin、静默刷新时复用已 mount 的插件实例（绝不替换为未挂载实例）；
 *  - 只有 active plugin id 变化才 oldPlugin.destroy() 并重建；
 *  - panel.routeMode 永远是模式 ID 字符串（driving/walking/transit/cycling）；
 *  - 所有路线/外部按钮由 capabilities 决定是否显示。
 */

import { getActiveMapPlugin, getActiveMapPluginId, MAP_ROUTE_MODES } from "./index.js";

// 统一模式 ID -> 中文标签（UI 文案只来自这里的元数据）
const MODE_LABEL = {
  driving: "驾车", walking: "步行", transit: "公交", cycling: "骑行",
};

export function createMapController(deps) {
  const { el, toast, asArray, categoryOf, buildGeoSvg } = deps;
  const getConfig = deps.getConfig || (() => ({}));
  const panels = new Map(); // geoKey -> panel

  const mapCfg = () => (getConfig() || {}).map_plugins || {};

  /** 插件当前声明支持的模式 ID 字符串数组。 */
  function supportedModeIds(plugin) {
    const ids = (plugin && plugin.capabilities && plugin.capabilities.routeModes) || ["driving"];
    return MAP_ROUTE_MODES.map((m) => m.id).filter((id) => ids.includes(id));
  }
  function modeLabel(id) { return MODE_LABEL[id] || id; }

  // ---------- 站点列表（外部打开/路线入口，按 capabilities 门禁）----------
  function buildSiteList(pane, cardEl, geo, geoKey) {
    const plugin = getActiveMapPlugin(mapCfg());
    const caps = (plugin && plugin.capabilities) || {};
    const cands = asArray(geo.candidates).filter((c) => Number.isFinite(+c.lat));
    const openLabel = plugin ? plugin.getExternalOpenLabel() : "在地图中打开";

    for (const c of cands) {
      const props = c.properties || c.props || {};
      const addr = c.address || props.address || "";
      const cat = categoryOf(c);
      const row = el("div", { class: "site-row" },
        el("div", { class: "site-main" },
          el("div", { class: "site-name" }, c.name || "候选地点"),
          el("div", { class: "site-meta" },
            c.distance_km != null ? el("span", {}, `直线 ${(+c.distance_km).toFixed(2)} km`) : null,
            cat ? el("span", {}, ` · ${cat}`) : null,
            addr ? el("span", { class: "muted" }, ` · ${addr}`) : null)));
      const btns = el("div", { class: "site-btns" });
      // 站内路线规划：仅在插件支持站内路线，且有中心点可作起点时显示；不调用外部打开
      if (caps.route === true && geo.center) {
        btns.appendChild(el("button", { class: "btn sm", onClick: () => {
          const mapTab = cardEl && cardEl.querySelector('.geo-tab[data-tab="map"]');
          if (mapTab) mapTab.click();
          planTo(geoKey, c);
        } }, "路线规划"));
      }
      // 外部地点打开：仅 externalSite 时显示；文字来自插件
      if (caps.externalSite === true) {
        btns.appendChild(el("button", { class: "btn sm ghost", onClick: () => openExternalSite(c) }, openLabel));
      }
      row.appendChild(btns);
      pane.appendChild(row);
    }
    if (!cands.length) pane.appendChild(el("div", { class: "muted" }, "无候选地点。"));
  }

  function activePlugin() { return getActiveMapPlugin(mapCfg()); }

  function openExternalSite(c) {
    const plugin = activePlugin();
    if (!plugin || plugin.capabilities.externalSite !== true || !Number.isFinite(+c.lat)) {
      toast("该地点缺少经纬度或当前地图插件不支持外部打开。", "warn");
      return;
    }
    try { window.open(plugin.openExternalSite(c), "_blank", "noopener"); }
    catch (e) { toast("打开外部地图失败。", "warn"); }
  }

  // ---------- 地图面板 ----------
  function buildMapPane(pane, geo, geoKey) {
    const cfg = mapCfg();
    const activeId = getActiveMapPluginId(cfg);
    const plugin = getActiveMapPlugin(cfg);

    // 无可用插件：SVG fallback（不暴露密钥）
    if (!plugin) {
      panels.delete(geoKey);
      pane.appendChild(el("div", { class: "map-notice" },
        el("div", { class: "map-notice-t" }, "地图插件不可用"),
        el("div", { class: "muted", style: "margin:4px 0 8px" },
          cfg.amap && cfg.amap.enabled !== false && !cfg.amap.js_key_configured
            ? "未配置地图 JS Key，显示离线示意图。配置后即可使用真实地图与路线（详见 .env.example）。"
            : "当前未启用可用的地图插件，显示离线示意图。")));
      const { svg } = buildGeoSvg(geo);
      pane.appendChild(svg);
      return;
    }

    const displayName = plugin.getDisplayName();
    const bottom = el("div", { class: "map-bottom" });
    let panel = panels.get(geoKey);

    // 插件 id 变化：销毁旧实例与旧 DOM，避免双地图/残留
    if (panel && panel.pluginId !== activeId) {
      try { panel.plugin && panel.plugin.destroy(); } catch (e) { /* ignore */ }
      if (panel.hostEl && panel.hostEl.parentNode) panel.hostEl.parentNode.removeChild(panel.hostEl);
      panels.delete(geoKey);
      panel = null;
    }

    if (panel && panel.hostEl) {
      // 静默刷新：同一插件实例仍已挂载，复用容器与实例，只重建底部 UI
      const host = panel.hostEl;
      pane.appendChild(host);
      pane.appendChild(bottom);
      panel.bottom = bottom;
      panel.geo = geo;
      safeResize(panel.plugin);
      renderBottom(panel);
      return;
    }

    const host = el("div", { class: "map-host", style: "height:440px;border-radius:10px;overflow:hidden;background:var(--bg-2)" });
    pane.appendChild(host);
    pane.appendChild(bottom);

    const modeIds = supportedModeIds(plugin);
    panel = {
      plugin, pluginId: activeId || plugin.id, hostEl: host, bottom,
      geo, mode: "normal", selected: null, routeTarget: null, routeResult: null,
      routeMode: modeIds[0] || "driving", // 永远是字符串 ID
    };
    panels.set(geoKey, panel);
    renderBottom(panel);

    // 初始化真实地图（异步）；失败回退 SVG，错误信息不含密钥
    plugin.mount(host).then(() => {
      plugin.showSites(geo, (cc) => { panel.selected = cc; renderBottom(panel); });
    }).catch((err) => {
      if (host.parentNode) host.parentNode.removeChild(host);
      if (bottom.parentNode) bottom.parentNode.removeChild(bottom);
      panels.delete(geoKey);
      pane.appendChild(el("div", { class: "map-notice" },
        el("div", { class: "map-notice-t" }, `${displayName}加载失败`),
        el("div", { class: "muted", style: "margin:4px 0 8px" }, String((err && err.message) || "地图加载失败")),
        el("div", { class: "muted", style: "margin-bottom:8px" }, "仍可使用下方地点列表与直线距离。")));
      const { svg } = buildGeoSvg(geo);
      pane.appendChild(svg);
    });
  }

  function safeResize(plugin) {
    try { if (typeof plugin.resize === "function") plugin.resize(); } catch (e) { /* ignore */ }
  }

  /** 从站点列表/外部入口发起站内路线规划。 */
  function planTo(geoKey, candidate) {
    const panel = panels.get(geoKey);
    if (!panel) { toast("地图尚未就绪。", "warn"); return; }
    const caps = (panel.plugin && panel.plugin.capabilities) || {};
    if (caps.route !== true || !panel.geo.center) { toast("当前地图插件不支持站内路线规划。", "warn"); return; }
    startRoute(panel, candidate);
  }

  function renderBottom(panel) {
    const bottom = panel.bottom;
    if (!bottom) return;
    bottom.innerHTML = "";
    const plugin = panel.plugin;
    const caps = plugin.capabilities || {};

    if (panel.mode === "route" && panel.routeTarget) { renderRouteBottom(panel); return; }

    const geo = panel.geo;
    const c = panel.selected;
    if (!c) {
      bottom.appendChild(el("div", { class: "map-hint muted" },
        `点击地图上的地点标记查看详情；Haversine 直线距离用于筛选，道路路线由${plugin.getDisplayName()}计算。`));
      return;
    }
    const props = c.properties || c.props || {};
    const addr = c.address || props.address || "";
    const cat = categoryOf(c);
    const card = el("div", { class: "map-site-card" },
      el("div", { class: "msc-name" }, c.name || "候选地点"),
      el("div", { class: "msc-rows" },
        c.distance_km != null ? el("div", {}, `直线距离：${(+c.distance_km).toFixed(2)} km`) : null,
        cat ? el("div", {}, `类别：${cat}`) : null,
        addr ? el("div", { class: "muted" }, `地址：${addr}`) : null));
    const actions = el("div", { class: "msc-actions" });
    if (caps.route === true && geo.center) {
      actions.appendChild(el("button", { class: "btn primary sm", onClick: () => startRoute(panel, c) }, "路线规划"));
    }
    if (caps.externalSite === true) {
      actions.appendChild(el("button", { class: "btn sm ghost", onClick: () => openExternalSite(c) }, plugin.getExternalOpenLabel()));
    }
    card.appendChild(actions);
    bottom.appendChild(card);
  }

  function startRoute(panel, c) {
    panel.mode = "route";
    panel.routeTarget = c;
    const ids = supportedModeIds(panel.plugin);
    if (!ids.includes(panel.routeMode)) panel.routeMode = ids[0] || "driving"; // 字符串
    panel.routeResult = { loading: true };
    renderBottom(panel);
    runRoute(panel);
  }

  function exitRoute(panel) {
    panel.mode = "normal";
    panel.routeTarget = null;
    panel.routeResult = null;
    try { panel.plugin.exitRoute(); } catch (e) { /* ignore */ }
    try { panel.plugin.showSites(panel.geo, (cc) => { panel.selected = cc; renderBottom(panel); }); }
    catch (e) { /* ignore */ }
    renderBottom(panel);
  }

  async function runRoute(panel) {
    const geo = panel.geo;
    const c = panel.routeTarget;
    const origin = { name: (geo.center && geo.center.name) || "起点", lat: +geo.center.lat, lon: +geo.center.lon };
    const destination = { name: c.name || "终点", lat: +c.lat, lon: +(c.lon != null ? c.lon : c.lng) };
    const mode = panel.routeMode; // 字符串 ID
    try {
      const res = await panel.plugin.planRoute({ origin, destination, mode });
      panel.routeResult = res || { ok: false, error: "暂时无法获取路线。" };
    } catch (e) {
      panel.routeResult = { ok: false, error: "暂时无法获取路线。" };
    }
    renderBottom(panel);
  }

  function renderRouteBottom(panel) {
    const bottom = panel.bottom;
    const plugin = panel.plugin;
    const caps = plugin.capabilities || {};
    const geo = panel.geo;
    const c = panel.routeTarget;

    bottom.appendChild(el("div", { class: "route-title" },
      el("strong", {}, (geo.center && geo.center.name) || "起点"),
      el("span", { class: "muted" }, " → "),
      el("strong", {}, c.name || "终点")));

    // 模式切换：只显示插件声明支持的模式，value 为字符串 ID
    const seg = el("div", { class: "route-modes" });
    for (const id of supportedModeIds(plugin)) {
      seg.appendChild(el("button", {
        type: "button",
        class: `route-mode${panel.routeMode === id ? " active" : ""}`,
        onClick: () => { panel.routeMode = id; panel.routeResult = { loading: true }; renderBottom(panel); runRoute(panel); },
      }, modeLabel(id)));
    }
    bottom.appendChild(seg);

    const info = el("div", { class: "route-info" });
    const res = panel.routeResult;
    if (!res || res.loading) {
      info.appendChild(el("div", { class: "muted" }, "正在请求路线…"));
    } else if (!res.ok) {
      info.appendChild(el("div", { class: "route-err" }, res.error || "暂时无法获取路线。"));
      info.appendChild(el("div", { class: "muted", style: "margin-top:4px" },
        `直线距离参考：${c.distance_km != null ? (+c.distance_km).toFixed(2) : "—"} km`));
    } else {
      info.appendChild(el("div", { class: "route-stat" },
        el("strong", {}, modeLabel(panel.routeMode)),
        el("span", {}, ` ${res.summary || ""}`)));
      if (res.steps && res.steps.length) {
        const det = el("details", { class: "route-detail" }, el("summary", {}, "查看详细换乘"));
        const ol = el("ol", { class: "route-steps" });
        res.steps.slice(0, 8).forEach((s) => ol.appendChild(el("li", {}, s)));
        det.appendChild(ol);
        info.appendChild(det);
      }
    }
    bottom.appendChild(info);

    const actions = el("div", { class: "msc-actions" });
    // 外部路线打开：仅 externalRoute 时显示
    if (caps.externalRoute === true && geo.center) {
      actions.appendChild(el("button", {
        class: "btn primary sm",
        onClick: () => {
          const url = plugin.openExternalRoute({
            origin: { name: (geo.center && geo.center.name) || "起点", lat: +geo.center.lat, lon: +geo.center.lon },
            destination: { name: c.name || "终点", lat: +c.lat, lon: +(c.lon != null ? c.lon : c.lng) },
            mode: panel.routeMode,
          });
          if (url) window.open(url, "_blank", "noopener");
        },
      }, plugin.getExternalOpenLabel()));
    }
    actions.appendChild(el("button", { class: "btn sm ghost", onClick: () => exitRoute(panel) }, "退出路线"));
    bottom.appendChild(actions);
  }

  return { buildMapPane, buildSiteList, planTo, _panels: panels };
}
