class VEl {
  constructor(tag) {
    this.tag = tag; this.children = []; this.attrs = {}; this._classes = new Set();
    this.style = {}; this.parentNode = null; this.hidden = false; this._listeners = {};
  }
  appendChild(c) {
    if (c == null) return c;
    for (const x of (Array.isArray(c) ? c : [c])) {
      if (x == null) continue;
      if (typeof x === "string") { this.children.push(x); continue; }
      x.parentNode = this; this.children.push(x);
    }
    return c;
  }
  set className(v) { this._classes = new Set(String(v).split(/\s+/).filter(Boolean)); }
  get className() { return [...this._classes].join(" "); }
  classList = {
    toggle: (c, on) => { const want = on === undefined ? !this._classes.has(c) : on;
      want ? this._classes.add(c) : this._classes.delete(c); return want; },
    add: (c) => this._classes.add(c), remove: (c) => this._classes.delete(c), contains: (c) => this._classes.has(c),
  };
  setAttribute(k, v) { this.attrs[k] = v; }
  addEventListener(type, fn) { (this._listeners[type] ||= []).push(fn); }
  removeChild(c) { const i = this.children.indexOf(c); if (i >= 0) { this.children.splice(i, 1); c.parentNode = null; } return c; }
  click() { (this._listeners.click || []).forEach((f) => f({ target: this })); }
  querySelector(sel) { return findOne(this, sel); }
  querySelectorAll(sel) { return findAll(this, sel); }
  get textContent() { return this.children.map((c) => (c instanceof VEl ? c.textContent : c)).join(""); }
  set textContent(v) { this.children = [String(v)]; }
  set innerHTML(v) { this.children = [String(v)]; }
  get innerHTML() { return this.textContent; }
}
function findAll(node, sel) {
  const out = [];
  const cls = sel.startsWith(".") ? sel.slice(1) : null;
  const walk = (n) => { for (const c of n.children) { if (!(c instanceof VEl)) continue;
    if ((cls && c._classes.has(cls)) || (!cls && c.tag === sel)) out.push(c); walk(c); } };
  walk(node); return out;
}
function findOne(node, sel) { return findAll(node, sel)[0] || null; }
function makeEl() {
  return (tag, attrs, ...kids) => {
    const node = new VEl(tag);
    if (attrs && typeof attrs === "object" && !Array.isArray(attrs) && !(attrs instanceof VEl)) {
      Object.entries(attrs).forEach(([k, v]) => {
        if (k === "class") node.className = v;
        else if (k === "hidden") node.hidden = !!v;
        else if (k === "onClick") node.addEventListener("click", v);
        else if (k !== "style") node.attrs[k] = v;
      });
    } else if (attrs != null) kids.unshift(attrs);
    for (const k of kids.flat(Infinity)) if (k != null) node.appendChild(k);
    return node;
  };
}

const Rec = { fitViewArgs: [], planners: [] };
function makeFakeAMap() {
  class Overlay { constructor() { this.clickFns = []; } on(ev, fn) { if (ev === "click") this.clickFns.push(fn); } getBounds() { return true; } }
  class Marker extends Overlay { constructor(o) { super(); this.opts = o; } }
  class Circle extends Overlay { constructor(o) { super(); this.opts = o; } }
  class LngLat { constructor(lng, lat) { this.lng = lng; this.lat = lat; } }
  class Map {
    constructor(container) { this.container = container; this.added = []; }
    add(o) { (Array.isArray(o) ? o : [o]).forEach((x) => this.added.push(x)); }
    remove(o) { (Array.isArray(o) ? o : [o]).forEach((x) => { const i = this.added.indexOf(x); if (i >= 0) this.added.splice(i, 1); }); }
    setFitView(overlays) { Rec.fitViewArgs.push(overlays);
      overlays.forEach((o) => { if (typeof o.getBounds !== "function") throw new Error("t[s].getBounds is not a function"); }); }
    destroy() { this.destroyed = true; } resize() {}
  }
  const mk = (kind) => class {
    constructor() { this.kind = kind; Rec.planners.push(this); }
    search(a, b, c, d) { (typeof c === "function" ? c : d)("complete", routeResult(kind)); }
    clear() {}
  };
  return { LngLat, Marker, Circle, Map,
    Driving: mk("driving"), Walking: mk("walking"), Riding: mk("riding"), Transfer: mk("transit"),
    TransferPolicy: { LEAST_TIME: 0 } };
}
function routeResult(kind) {
  if (kind === "transit") return { plans: [{ distance: 8200, time: 1800, segments: [
    { walking: { distance: 300 }, on_station: { name: "ZHONGGUANCUN" }, off_station: { name: "HAIDIAN" } }] }] };
  return { routes: [{ distance: 5000, time: 900 }] };
}

const store = new Map();
globalThis.localStorage = { getItem: (k) => (store.has(k) ? store.get(k) : null), setItem: (k, v) => store.set(k, String(v)), removeItem: (k) => store.delete(k) };
const appendedScripts = [];
globalThis.window = globalThis.window || {};
// setTimeout 保持真实（flush 用微任务）；amap 的 15s 超时在测试窗口内不触发
globalThis.document = {
  head: { appendChild: (n) => { appendedScripts.push(n); if (globalThis.__scriptFail && n.onerror) n.onerror(); } },
  createElement: (tag) => tag === "script"
    ? { tag, _src: "", set src(v) { this._src = v; }, get src() { return this._src; }, async: false, onerror: null }
    : new VEl(tag),
};

const amap = await import("../longflow/web/maps/amap.js");
const maps = await import("../longflow/web/maps/index.js");
const panelMod = await import("../longflow/web/maps/panel.js");

let fail = 0;
const ok = (cond, msg) => { if (!cond) { fail++; console.log("FAIL:", msg); } else console.log("PASS:", msg); };
const tick = () => Promise.resolve();
async function flush(n = 40) { for (let i = 0; i < n; i++) await Promise.resolve(); }
const findBtn = (root, text) => findAll(root, "button").find((b) => b.textContent === text || b.textContent.includes(text));
const LBL = { driving: "驾车", walking: "步行", transit: "公交", cycling: "骑行" };

function makeController(config) {
  globalThis.window.AMap = FakeAMap;
  try { globalThis.localStorage.removeItem("longflow.mapPlugin"); } catch {}
  const ctl = panelMod.createMapController({
    el: makeEl(), toast: () => {},
    asArray: (v) => Array.isArray(v) ? v : (v == null ? [] : [v]),
    categoryOf: () => "", buildGeoSvg: () => ({ svg: new VEl("svg") }),
    getConfig: () => ({ map_plugins: config }),
  });
  return ctl;
}
const GEO = {
  center: { name: "CENTER_STATION", lat: 39.9835, lon: 116.3192 }, radius_km: 2, crs: "EPSG:4326",
  candidates: [
    { name: "COFFEE_A", lat: 39.984, lon: 116.320, distance_km: 0.3 },
    { name: "COFFEE_B", lat: 39.982, lon: 116.318, distance_km: 0.5 },
  ],
};
const FakeAMap = makeFakeAMap();
globalThis.window.AMap = FakeAMap;

// T1/2 setFitView overlays
{
  const ctl = makeController({ active: "amap", amap: { enabled: true, js_key: "JSK", city: "BJ" } });
  const pane = new VEl("div");
  let threw = false;
  try { ctl.buildMapPane(pane, GEO, "k1"); } catch (e) { threw = true; console.log("ERR", e && e.message); }
  await flush();
  ok(!threw, "T1: buildMapPane no throw");
  const last = Rec.fitViewArgs[Rec.fitViewArgs.length - 1];
  ok(Array.isArray(last) && last.length >= 2, "T1: setFitView got overlay array");
  ok(last.every((o) => o instanceof FakeAMap.Marker || o instanceof FakeAMap.Circle), "T1: all Marker/Circle (no LngLat)");
  ok(!last.some((o) => o instanceof FakeAMap.LngLat), "T1: no LngLat");
  const panel = ctl._panels.get("k1");
  ok(panel && panel.plugin._map instanceof FakeAMap.Map, "T1: map mounted");
}

// T3/4/5/8/10/11/14 full flow
{
  const ctl = makeController({ active: "amap", amap: { enabled: true, js_key: "JSK", city: "BJ" } });
  const pane = new VEl("div"); const card = new VEl("div"); const listPane = new VEl("div");
  ctl.buildMapPane(pane, GEO, "gk");
  ctl.buildSiteList(listPane, card, GEO, "gk");
  await flush();
  const panel = ctl._panels.get("gk");
  const plugin = panel.plugin;
  ok(listPane.textContent.includes("路线规划"), "site list shows route button");
  ok(listPane.textContent.includes(plugin.getExternalOpenLabel()), "site list external label from plugin");

  const candMarker = plugin._layers.filter((o) => o instanceof FakeAMap.Marker).find((m) => m.opts.title === GEO.candidates[0].name);
  ok(!!candMarker, "candidate marker exists");
  candMarker.clickFns.forEach((f) => f());
  await flush();
  ok(panel.selected && panel.selected.name === GEO.candidates[0].name, "marker click selects candidate");

  findBtn(panel.bottom, "路线规划").click();
  await flush();
  ok(Rec.planners[Rec.planners.length - 1] instanceof FakeAMap.Driving, "T3: first route -> Driving (driving string)");
  ok(panel.routeMode === "driving" && typeof panel.routeMode === "string", "T3: routeMode is string driving");
  ok(panel.routeResult.ok === true, "T8: planRoute ok");
  ok(panel.routeResult.distanceMeters === 5000 && panel.routeResult.durationSeconds === 900, "T8: distance/duration consumed");
  ok(typeof panel.routeResult.summary === "string" && panel.routeResult.summary.length > 0, "T8: summary present");

  for (const [id, Cls] of [["walking", FakeAMap.Walking], ["transit", FakeAMap.Transfer], ["cycling", FakeAMap.Riding]]) {
    findBtn(panel.bottom, LBL[id]).click();
    await flush();
    ok(Rec.planners[Rec.planners.length - 1] instanceof Cls, "T4: " + id + " -> planner");
    ok(panel.routeMode === id, "T4: routeMode === " + id);
  }
  findBtn(panel.bottom, LBL.transit).click();
  await flush();
  ok(panel.routeResult.ok === true && panel.routeResult.mode === "transit", "T14: transit plans success not failed");
  ok(panel.routeResult.distanceMeters === 8200 && panel.routeResult.durationSeconds === 1800, "T14: transit dist/dur");
  ok(panel.routeResult.steps.length >= 1, "T14: transit steps extracted");
  ok(!!findBtn(panel.bottom, plugin.getExternalOpenLabel()), "T11: external route label from plugin");

  findBtn(panel.bottom, "退出路线").click();
  await flush();
  ok(panel.mode === "normal", "exit route -> normal");
  ok(plugin._layers.length === 4, "markers+circle restored after exit (4)");

  const mapBefore = plugin._map;
  const pane2 = new VEl("div");
  ctl.buildMapPane(pane2, GEO, "gk");
  await flush();
  const p2 = ctl._panels.get("gk");
  ok(p2.plugin === plugin && p2.plugin._map === mapBefore, "T5: silent refresh reuses mounted instance");
  ctl.planTo("gk", GEO.candidates[0]);
  await flush();
  ok(p2.mode === "route" && p2.routeResult && p2.routeResult.error !== "地图未挂载", "T5: route works after refresh, no 'map not mounted'");
}

// T6/7/10/11 switch + custom no available
{
  let destroys = 0, mounts = 0;
  maps.registerMapPlugin({
    id: "custom-map", name: "My Map", version: "1.0.0",
    capabilities: { map: true, markers: true, route: true, routeModes: ["driving", "walking"], externalSite: true, externalRoute: true },
    create() {
      return { id: "custom-map",
        capabilities: { map: true, markers: true, route: true, routeModes: ["driving", "walking"], externalSite: true, externalRoute: true },
        async mount() { mounts++; }, showSites() {},
        async planRoute({ mode }) { return { provider: "custom-map", mode, ok: true, distanceMeters: 1200, durationSeconds: 900, summary: "1.2 km / 15 min", steps: [] }; },
        exitRoute() {}, openExternalSite: (s) => "https://example.com/?q=" + encodeURIComponent(s.name), openExternalRoute: () => "https://example.com/route",
        getDisplayName: () => "My Map", getExternalOpenLabel: () => "OPEN_IN_MY_MAP", destroy() { destroys++; } };
    },
  });
  const ctl = makeController({ active: "amap", amap: { enabled: true, js_key: "JSK" } });
  const pane = new VEl("div");
  ctl.buildMapPane(pane, GEO, "sw");
  await flush();
  const amapInst = ctl._panels.get("sw").plugin;
  ok(amapInst.id === "amap", "initial amap");
  maps.setActiveMapPlugin("custom-map");
  const pane2 = new VEl("div");
  ctl.buildMapPane(pane2, GEO, "sw");
  await flush();
  const cp = ctl._panels.get("sw");
  ok(cp.plugin.id === "custom-map", "switched to custom plugin");
  ok(amapInst._map === null, "T6: old amap destroy() called");
  ok(destroys === 0 && mounts === 1, "T7: custom plugin mounted without explicit available");
  cp.selected = GEO.candidates[0];
  ctl.planTo("sw", GEO.candidates[0]);
  await flush();
  ok(cp.routeResult && cp.routeResult.provider === "custom-map", "T8: custom unified RouteResult consumed by MapPanel");
  const btns = findAll(cp.bottom, "button").map((b) => b.textContent);
  ok(btns.includes(LBL.driving) && btns.includes(LBL.walking) && !btns.includes(LBL.transit) && !btns.includes(LBL.cycling), "T10: partial routeModes only driving/walking");
  ok(btns.includes("OPEN_IN_MY_MAP"), "T11: external label changes with plugin");
  maps.setActiveMapPlugin(null);
}

// T9 no-route plugin
{
  maps.registerMapPlugin({
    id: "noroute", name: "NoRoute", version: "1.0.0",
    capabilities: { map: true, markers: true, route: false, routeModes: [], externalSite: true, externalRoute: false },
    create() {
      return { id: "noroute",
        capabilities: { map: true, markers: true, route: false, routeModes: [], externalSite: true, externalRoute: false },
        async mount() {}, showSites() {}, async planRoute() { return { ok: false }; }, exitRoute() {},
        openExternalSite: () => "u", openExternalRoute: () => "",
        getDisplayName: () => "NoRoute", getExternalOpenLabel: () => "OPEN_IN_NOROUTE", destroy() {} };
    },
  });
  maps.setActiveMapPlugin("noroute");
  const ctl = makeController({ active: "noroute" });
  const pane = new VEl("div"); const listPane = new VEl("div"); const card = new VEl("div");
  ctl.buildMapPane(pane, GEO, "nr");
  ctl.buildSiteList(listPane, card, GEO, "nr");
  await flush();
  const panel = ctl._panels.get("nr");
  ctl.planTo("nr", GEO.candidates[0]);
  await flush();
  ok(panel.mode !== "route", "T9: no-route plugin does not enter route mode");
  ok(!listPane.textContent.includes("路线规划"), "T9: site list hides in-app route button");
  ok(listPane.textContent.includes("OPEN_IN_NOROUTE"), "T9: external site button still shown");
  maps.setActiveMapPlugin(null);
}

// T12 URL no double encoding (ASCII names; verify %25 not present)
{
  const f = { name: "ZHONGGUANCUN_STATION", lat: 39.98, lon: 116.31 };
  const t = { name: "COFFEE_SHOP", lat: 39.99, lon: 116.32 };
  const u = new URL(amap.buildExternalRouteUrl(f, t, "driving"));
  const from = u.searchParams.get("from");
  ok(from.includes("ZHONGGUANCUN_STATION") && !from.includes("%25"), "T12: from name not double-encoded: " + from);
  ok(new URL(amap.buildExternalSiteUrl(f)).searchParams.get("name") === "ZHONGGUANCUN_STATION", "T12: marker name readable");
  // Chinese double-encoding guard: a Chinese name must decode back to itself
  const fc = { name: "中关村地铁站", lat: 39.98, lon: 116.31 };
  const uc = new URL(amap.buildExternalSiteUrl(fc));
  ok(uc.searchParams.get("name") === "中关村地铁站", "T12: Chinese name decodes to readable Chinese (no double encode)");
}

// T13 HTML escape
{
  const evil = '<img src=x onerror=alert(1)>';
  ok(amap.escapeHtml(evil).includes("&lt;img") && !amap.escapeHtml(evil).includes("<img"), "T13: escapeHtml");
  const plugin = maps.getActiveMapPlugin({ active: "amap", amap: { js_key: "JSK" } });
  plugin._AMap = FakeAMap; plugin._map = new FakeAMap.Map(new VEl("div"));
  plugin.showSites({ center: { name: evil, lat: 39.98, lon: 116.31 }, candidates: [], radius_km: 1 }, () => {});
  const cm = plugin._layers.find((o) => o instanceof FakeAMap.Marker);
  ok(!cm.opts.content.includes("<img src=x") && cm.opts.content.includes("&lt;img"), "T13: center marker escaped");
  await plugin.planRoute({ origin: { name: evil, lat: 39.98, lon: 116.31 }, destination: { name: "END", lat: 39.99, lon: 116.32 }, mode: "driving" });
  const rc = (plugin._routeLayers || []).map((m) => m.opts.content).join("");
  ok(!rc.includes("<img src=x") && rc.includes("&lt;img"), "T13: route markers escaped");
}

// T15/16/18 security
{
  delete globalThis.window.AMap;
  delete globalThis.window._AMapSecurityConfig;
  appendedScripts.length = 0;
  amap.loadAMap({ jsKey: "KEY", securityCode: "SUPER_SECRET_42" }).catch(() => {});
  ok(globalThis.window._AMapSecurityConfig && globalThis.window._AMapSecurityConfig.securityJsCode === "SUPER_SECRET_42", "T15: securityJsCode set");
  const script = appendedScripts.find((s) => s._src.includes("webapi.amap.com"));
  ok(!!script, "T15: SDK script inserted");
  ok(!script._src.includes("SUPER_SECRET_42"), "T18: script URL has no securityCode");
  // 结束这个模拟的 SDK 加载，让后续失败分支各自独立，不等待真实的 15 秒超时。
  if (script && typeof script.onerror === "function") script.onerror();
  await flush();
  let errMsg = "";
  try { await amap.loadAMap({ jsKey: "", securityCode: "SUPER_SECRET_42" }); } catch (e) { errMsg = String(e && e.message || e); }
  ok(!errMsg.includes("SUPER_SECRET_42"), "T18: error text has no securityCode: " + errMsg);
  delete globalThis.window._AMapSecurityConfig;
  let crashed = false;
  globalThis.__scriptFail = true;
  try { await amap.loadAMap({ jsKey: "K" }).catch(() => {}); } catch { crashed = true; }
  globalThis.__scriptFail = false;
  ok(!crashed, "T16: no securityCode does not crash");
  globalThis.window.AMap = FakeAMap;
}

// T20 fallback
{
  // 无 JS Key：插件不可用，同步 SVG fallback（不触发 SDK 加载）
  // 无 Key 且无其他可用插件：临时反注册自定义插件，模拟"所有地图插件不可用"
  maps.unregisterMapPlugin("custom-map");
  maps.unregisterMapPlugin("noroute");
  maps.setActiveMapPlugin(null);
  let ctl = makeController({ active: "amap", amap: { enabled: true } }); // 无 js_key
  const pane = new VEl("div");
  ctl.buildMapPane(pane, GEO, "nokey");
  await flush(8);
  ok(findAll(pane, "svg").length >= 1, "T20: no key -> SVG fallback");
  ok(pane.textContent.includes("地图插件不可用") || pane.textContent.includes("JS Key"), "T20: no key notice shown");
  ok(!ctl._panels.get("nokey"), "T20: no panel cached when unavailable");

  // SDK 加载失败：有 js_key 但脚本 onerror -> mount reject -> SVG fallback
  ctl = makeController({ active: "amap", amap: { enabled: true, js_key: "KEY" } });
  delete globalThis.window.AMap; // 强制走 loadAMap（非早返回）
  globalThis.__scriptFail = true;
  const pane2 = new VEl("div");
  ctl.buildMapPane(pane2, GEO, "sdkfail");
  await flush(32);
  ok(findAll(pane2, "svg").length >= 1, "T20: SDK fail -> SVG fallback");
  ok(pane2.textContent.includes("加载失败"), "T20: SDK fail notice shown");
  ok(!pane2.textContent.includes("SUPER_SECRET"), "T20: fallback text has no secret");
  globalThis.__scriptFail = false;
  globalThis.window.AMap = FakeAMap;
}

{
  // 元数据列表：Google coming soon；无 available 声明的第三方插件默认可用
  maps.registerMapPlugin({
    id: "meta-custom", name: "Meta", version: "1.0.0",
    capabilities: { map: true, markers: true, route: true, routeModes: ["driving"], externalSite: true, externalRoute: true },
    create() { return { id: "meta-custom", async mount() {}, showSites() {}, async planRoute() { return { ok: true }; },
      exitRoute() {}, openExternalSite: () => "u", openExternalRoute: () => "u",
      getDisplayName: () => "Meta", getExternalOpenLabel: () => "open", destroy() {} }; },
  });
  const list = maps.listMapPlugins({ active: "amap", amap: { js_key_configured: true } });
  const g = list.find((x) => x.id === "google");
  ok(g.coming_soon && g.available === false, "google coming soon unavailable");
  const cust = list.find((x) => x.id === "meta-custom");
  ok(cust && cust.available === true, "custom plugin (no available declared) listed available by default");
}

// T18b: loadAMap / 错误路径不通过 console 输出 securityCode
{
  const SECRET = "CONSOLE_SECRET_777";
  const captured = [];
  const origLog = console.log, origErr = console.error, origWarn = console.warn;
  console.log = (...a) => captured.push(a.join(" "));
  console.error = (...a) => captured.push(a.join(" "));
  console.warn = (...a) => captured.push(a.join(" "));
  delete globalThis.window.AMap;
  delete globalThis.window._AMapSecurityConfig;
  amap.loadAMap({ jsKey: "K2", securityCode: SECRET }).catch(() => {});
  // 无 key 触发失败路径
  amap.loadAMap({ jsKey: "", securityCode: SECRET }).catch(() => {});
  await flush(8);
  console.log = origLog; console.error = origErr; console.warn = origWarn;
  const blob = captured.join("\n");
  ok(!blob.includes(SECRET), "T18: console 输出不含 securityCode");
  globalThis.window.AMap = FakeAMap;
}
console.log(fail ? `\n${fail} FAILED` : "\nALL PASS");
process.exit(fail ? 1 : 0);
