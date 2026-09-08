/* Map Plugin 通用能力 + 高德安全配置测试（node 直接跑，无浏览器依赖；
 * 用最小 DOM stub 验证 window._AMapSecurityConfig 在 SDK <script> 插入前已设置）。 */

const appendLog = [];
globalThis.window = globalThis.window || {};
// localStorage stub（node 无浏览器存储；注册表手动选择依赖它）
const _store = new Map();
globalThis.localStorage = {
  getItem: (k) => (_store.has(k) ? _store.get(k) : null),
  setItem: (k, v) => _store.set(k, String(v)),
  removeItem: (k) => _store.delete(k),
};
const _origSetTimeout = globalThis.setTimeout;
globalThis.setTimeout = () => 0;
globalThis.document = {
  head: {
    appendChild(node) {
      appendLog.push({ hasSecurityAtAppend: !!globalThis.window._AMapSecurityConfig, src: node._src || "" });
    },
  },
  createElement(tag) {
    return { tag, _src: "", set src(v) { this._src = v; }, get src() { return this._src; }, async: false };
  },
};

const amap = await import("../longflow/web/maps/amap.js");
const maps = await import("../longflow/web/maps/index.js");

let fail = 0;
const ok = (cond, msg) => { if (!cond) { fail++; console.log("FAIL:", msg); } else console.log("PASS:", msg); };

// T1: securityCode 在 script 插入前设置
{
  delete globalThis.window.AMap;
  delete globalThis.window._AMapSecurityConfig;
  appendLog.length = 0;
  const _p1 = amap.loadAMap({ jsKey: "TESTKEY", securityCode: "SECRET123" }); _p1.catch(() => {});
  const sec = globalThis.window._AMapSecurityConfig;
  const scriptAppends = appendLog.filter((x) => x.src.includes("webapi.amap.com"));
  ok(!!sec && sec.securityJsCode === "SECRET123", "T1: _AMapSecurityConfig.securityJsCode 已设置");
  ok(scriptAppends.length === 1, "T1: SDK <script> 插入一次");
  ok(scriptAppends[0] && scriptAppends[0].hasSecurityAtAppend === true, "T1: 插入 <script> 时安全配置已存在（顺序正确）");
  ok(scriptAppends[0].src.includes("key=TESTKEY") && !scriptAppends[0].src.includes("SECRET123"), "T1: URL 含 JS Key、不含 securityCode");
}

// T2: 无 securityCode 不崩、不设置
{
  globalThis.window.AMap = { __fake: true };
  delete globalThis.window._AMapSecurityConfig;
  let crashed = false;
  try { await amap.loadAMap({ jsKey: "k" }); } catch { crashed = true; }
  ok(crashed === false, "T2: 无 securityCode 时不崩溃");
  ok(globalThis.window._AMapSecurityConfig === undefined, "T2: 未配置则不设置 _AMapSecurityConfig");
  delete globalThis.window.AMap;
}

// 坐标转换
{
  const g = amap.wgs84ToGcj02(116.3192, 39.9835);
  ok(Math.abs(g.lng - 116.3192) > 0.001 && Math.abs(g.lat - 39.9835) > 0.001, "GCJ02 境内偏移");
  const o = amap.wgs84ToGcj02(-73.98, 40.75);
  ok(o.lng === -73.98 && o.lat === 40.75, "境外坐标不转换");
}

// 统一路线模式映射
{
  const f = { name: "A", lat: 39.98, lon: 116.31 };
  const t = { name: "B", lat: 39.99, lon: 116.32 };
  ok(amap.buildExternalRouteUrl(f, t, "driving").includes("type=car"), "driving → car");
  ok(amap.buildExternalRouteUrl(f, t, "walking").includes("type=walk"), "walking → walk");
  ok(amap.buildExternalRouteUrl(f, t, "transit").includes("type=bus"), "transit → bus(Transfer)");
  ok(amap.buildExternalRouteUrl(f, t, "cycling").includes("type=ride"), "cycling → ride(Riding)");
  ok(amap.buildExternalSiteUrl(f).includes("uri.amap.com/marker"), "地点跳转 URI");
}

// 注册表 / active / 自定义插件
{
  const inst = maps.getActiveMapPlugin({ active: "amap", amap: { enabled: true, js_key: "x" }, google: { coming_soon: true } });
  ok(inst && inst.id === "amap", "T6: active=amap 返回 AMapPlugin");
  ok(maps.getActiveMapPlugin({ active: "amap", amap: { enabled: true } }) === null, "无 JS key → 无可用插件");

  let destroyed = 0, mounted = 0;
  maps.registerMapPlugin({
    id: "custom-map", name: "My Map", version: "1.0.0", available: true,
    capabilities: { map: true, markers: true, circle: false, route: false, routeModes: [], externalSite: true, externalRoute: false },
    configSchema: {},
    create() {
      return {
        id: "custom-map", available: true,
        capabilities: { map: true, markers: true, circle: false, route: false, routeModes: [], externalSite: true, externalRoute: false },
        async mount() { mounted++; },
        showSites() {},
        async planRoute() { return { provider: "custom-map", ok: true, distanceMeters: 1000, durationSeconds: 600, summary: "1.0 km · 10 min" }; },
        exitRoute() {},
        openExternalSite(s) { return "https://example.com/map?" + encodeURIComponent(s.name); },
        openExternalRoute() { return ""; },
        getDisplayName() { return "My Map"; },
        getExternalOpenLabel() { return "在 My Map 打开"; },
        destroy() { destroyed++; },
      };
    },
  });
  maps.setActiveMapPlugin("custom-map");
  const cust = maps.getActiveMapPlugin({ active: "amap", amap: { js_key: "x" } });
  ok(cust && cust.id === "custom-map", "T7: active=custom-map 使用自定义插件");
  await cust.mount();
  ok(mounted === 1, "T7: customMap.mount 被调用");
  const rr = await cust.planRoute({ origin: { name: "o" }, destination: { name: "d" }, mode: "driving" });
  ok(rr.provider === "custom-map" && rr.summary === "1.0 km · 10 min", "T8: planRoute 返回统一 RouteResult");
  ok(cust.capabilities.route === false && cust.capabilities.routeModes.length === 0, "T9: 不支持 route → UI 隐藏路线按钮");
  ok(cust.getExternalOpenLabel() === "在 My Map 打开", "T10: 自定义外部打开文字");
  cust.destroy();
  ok(destroyed === 1, "T11: destroy() 可清理旧实例");

  maps.setActiveMapPlugin(null);
  const a = maps.getActiveMapPlugin({ active: "amap", amap: { js_key: "x" } });
  ok(a.getExternalOpenLabel() === "在高德地图打开", "T10: 高德外部打开文字");
  ok(a.getDisplayName() === "高德地图", "高德 displayName");
  ok(JSON.stringify(a.capabilities.routeModes) === JSON.stringify(["driving", "walking", "transit", "cycling"]), "T12: 高德四种统一模式");
}

{
  const list = maps.listMapPlugins({ active: "amap", amap: { js_key_configured: true, city: "北京" }, google: {} });
  const ids = list.map((x) => x.id);
  ok(ids.includes("amap") && ids.includes("google") && ids.includes("custom-map"), "列表含内置与自定义插件");
  const g = list.find((x) => x.id === "google");
  ok(g.coming_soon === true && g.available === false, "Google Coming Soon 且不可用");
}

console.log(fail ? `\n${fail} 项失败` : "\n全部通过");
process.exit(fail ? 1 : 0);
