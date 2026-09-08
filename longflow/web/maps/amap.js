/* 高德地图插件（AMap）—— 默认内置地图实现。
 *
 * 职责边界：Agent/工作台负责搜索、筛选、Haversine 直线距离与分析；
 * 本插件负责真实底图、Marker、Circle、道路路线计算/绘制、外部导航跳转。
 * 不实现任何道路图/寻路/Polyline 算法，全部交给 AMap JS API。
 *
 * 坐标：Agent 产出 WGS84(EPSG:4326)，高德使用 GCJ-02，转换只发生在本文件内部。
 *
 * 安全配置（高德 JS API 2.0）：Demo 使用 securityJsCode 客户端方式 —— 必须在加载
 * SDK <script> 之前设置 window._AMapSecurityConfig = { securityJsCode }。该值通过
 * /api/config 下发（浏览器加载 JS API 本就需要；生产建议改 serviceHost 代理，见 README）。
 * AMAP_WEB_KEY 是服务端 Web Service Key，绝不进入浏览器。
 */

// ============ 纯函数（无浏览器依赖，可单测）============

const AMAP_TYPE_BY_MODE = { driving: "car", walking: "walk", transit: "bus", cycling: "ride" };

/** 轻量 HTML 转义：动态名称写入 Marker content HTML 前必须转义，防止注入。 */
export function escapeHtml(s) {
  return String(s == null ? "" : s)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

/**
 * 高德地点 URI（输入已是 GCJ-02 坐标；手机浏览器优先唤起 App）。
 * 名称使用原始字符串，由 URLSearchParams 完成唯一一次编码。
 */
export function buildExternalSiteUrl(p) {
  const lng = p.lng != null ? p.lng : p.lon;
  const name = p.name || "地点";
  const q = new URLSearchParams({
    position: `${+lng},${+p.lat}`,
    name, // 原始字符串；URLSearchParams 负责编码
    src: "longflow",
    coordinate: "gaode",
    callnative: "1",
  });
  return `https://uri.amap.com/marker?${q.toString()}`;
}

/**
 * 高德路线 URI（输入已是 GCJ-02 坐标；mode: driving|walking|transit|cycling）。
 * from/to 使用高德 navigation 约定格式 "lng,lat,name"，名称保持原始中文（不预先编码）。
 */
export function buildExternalRouteUrl(f, t, mode = "driving") {
  const flng = f.lng != null ? f.lng : f.lon;
  const tlng = t.lng != null ? t.lng : t.lon;
  const type = AMAP_TYPE_BY_MODE[mode] || "car";
  const q = new URLSearchParams({
    src: "longflow",
    coordinate: "gaode",
    callnative: "1",
    from: `${+flng},${+f.lat},${f.name || "起点"}`,
    to: `${+tlng},${+t.lat},${t.name || "终点"}`,
    mode: "route",
    type,
  });
  return `https://uri.amap.com/navigation?${q.toString()}`;
}

const PI = Math.PI;
const A = 6378245.0;
const EE = 0.00669342162296594323;

function _tlat(x, y) {
  let r = -100.0 + 2.0 * x + 3.0 * y + 0.2 * y * y + 0.1 * x * y + 0.2 * Math.sqrt(Math.abs(x));
  r += ((20.0 * Math.sin(6.0 * x * PI) + 20.0 * Math.sin(2.0 * x * PI)) * 2.0) / 3.0;
  r += ((20.0 * Math.sin(y * PI) + 40.0 * Math.sin((y / 3.0) * PI)) * 2.0) / 3.0;
  r += ((160.0 * Math.sin((y / 12.0) * PI) + 320 * Math.sin((y * PI) / 30.0)) * 2.0) / 3.0;
  return r;
}
function _tlng(x, y) {
  let r = 300.0 + x + 2.0 * y + 0.1 * x * x + 0.1 * x * y + 0.1 * Math.sqrt(Math.abs(x));
  r += ((20.0 * Math.sin(6.0 * x * PI) + 20.0 * Math.sin(2.0 * x * PI)) * 2.0) / 3.0;
  r += ((20.0 * Math.sin(x * PI) + 40.0 * Math.sin((x / 3.0) * PI)) * 2.0) / 3.0;
  r += ((150.0 * Math.sin((x / 12.0) * PI) + 300.0 * Math.sin((x / 30.0) * PI)) * 2.0) / 3.0;
  return r;
}
function _outOfChina(lng, lat) {
  return lng < 72.004 || lng > 137.8347 || lat < 0.8293 || lat > 55.8271;
}

/** WGS84(GPS/GeoJSON, EPSG:4326) → GCJ-02（高德火星坐标）。境外不转换。 */
export function wgs84ToGcj02(lng, lat) {
  if (typeof lng !== "number" || typeof lat !== "number" || _outOfChina(lng, lat)) return { lng, lat };
  let dLat = _tlat(lng - 105.0, lat - 35.0);
  let dLng = _tlng(lng - 105.0, lat - 35.0);
  const radLat = (lat / 180.0) * PI;
  let magic = Math.sin(radLat);
  magic = 1 - EE * magic * magic;
  const sm = Math.sqrt(magic);
  dLat = (dLat * 180.0) / (((A * (1 - EE)) / (magic * sm)) * PI);
  dLng = (dLng * 180.0) / ((A / sm) * Math.cos(radLat) * PI);
  return { lng: lng + dLng, lat: lat + dLat };
}

function formatKm(meters) {
  const m = Number(meters);
  if (!isFinite(m)) return "距离未知";
  return m >= 1000 ? `${(m / 1000).toFixed(1)} km` : `${Math.round(m)} m`;
}
function formatMin(seconds) {
  const s = Number(seconds);
  if (!isFinite(s)) return "时间未知";
  return `${Math.max(1, Math.round(s / 60))} min`;
}

// ============ SDK Loader（Promise 缓存；安全配置必须先于 script）============

let _loadingPromise = null;

/**
 * 加载高德 JS SDK。
 * @param {{jsKey:string, securityCode?:string, serviceHost?:string}} cfg
 * 顺序：先设置 window._AMapSecurityConfig，再创建并插入 <script>。
 * 不主动 console.log 任何密钥。
 */
export function loadAMap(cfg) {
  if (typeof window !== "undefined" && window.AMap) return Promise.resolve(window.AMap);
  if (_loadingPromise) return _loadingPromise;
  const { jsKey, securityCode, serviceHost } = cfg || {};
  _loadingPromise = new Promise((resolve, reject) => {
    if (typeof window === "undefined" || typeof document === "undefined") {
      reject(new Error("地图 SDK 只能在浏览器中加载")); return;
    }
    if (!jsKey) { reject(new Error("未配置地图 JS Key")); return; }
    // ① 安全配置必须在 SDK script 插入之前设置
    if (serviceHost) {
      window._AMapSecurityConfig = { serviceHost };
    } else if (securityCode) {
      window._AMapSecurityConfig = { securityJsCode: securityCode };
    }
    // ② 之后才创建 script
    const cb = "__amap_init__" + Math.random().toString(36).slice(2);
    window[cb] = () => { resolve(window.AMap); try { delete window[cb]; } catch { window[cb] = undefined; } };
    const s = document.createElement("script");
    s.src = `https://webapi.amap.com/maps?v=2.0&key=${encodeURIComponent(jsKey)}`
      + `&plugin=AMap.Driving,AMap.Walking,AMap.Transfer,AMap.Riding&callback=${cb}`;
    s.async = true;
    s.onerror = () => reject(new Error("地图 SDK 加载失败（网络异常或 Key 无效）"));
    document.head.appendChild(s);
    if (typeof setTimeout === "function") {
      setTimeout(() => { if (!window.AMap) reject(new Error("地图 SDK 加载超时")); }, 15000);
    }
  }).catch((e) => { _loadingPromise = null; throw e; });
  return _loadingPromise;
}

// ============ 插件实例 ============

class AMapPluginInstance {
  constructor(cfg = {}) {
    this.id = "amap";
    this._cfg = cfg;
    this._map = null;
    this._layers = [];
    this._routeLayers = null;
    this._planner = null;
    this.capabilities = {
      map: true, markers: true, circle: true, route: true,
      routeModes: ["driving", "walking", "transit", "cycling"],
      externalSite: true, externalRoute: true,
    };
    this.available = cfg.enabled !== false && !!cfg.js_key;
    this.reasonUnavailable = !cfg.js_key ? "未配置地图 JS Key" : (cfg.enabled === false ? "插件已禁用" : "");
  }

  getDisplayName() { return "高德地图"; }
  getExternalOpenLabel() { return "在高德地图打开"; }

  async mount(container) {
    if (this._map) return;
    const AMap = await loadAMap({
      jsKey: this._cfg.js_key,
      securityCode: this._cfg.security_code, // Demo：securityJsCode 客户端方式
    });
    this._AMap = AMap;
    this._map = new AMap.Map(container, { zoom: 13, viewMode: "2D" });
  }

  /** 普通模式：中心 Marker + 候选 Marker + 半径 Circle，自动调整视野。 */
  showSites(geo, onSelect) {
    const AMap = this._AMap, map = this._map;
    if (!map) throw new Error("地图未挂载");
    this.exitRoute(true);
    this._clearLayers();
    const overlays = []; // 只收集 Marker/Circle 等覆盖物对象（setFitView 不接受 LngLat）
    const toGcjLL = (c) => {
      const lon = c.lon != null ? +c.lon : +c.lng;
      const g = wgs84ToGcj02(lon, +c.lat);
      return new AMap.LngLat(g.lng, g.lat);
    };
    const center = geo.center && isFinite(+geo.center.lat) ? geo.center : null;

    if (center) {
      const cm = new AMap.Marker({
        position: toGcjLL(center),
        content: '<div style="transform:translate(-50%,-100%);color:#fff;background:#e5484d;font-weight:700;padding:2px 7px;border-radius:8px;white-space:nowrap;box-shadow:0 1px 4px rgba(0,0,0,.3);">★ ' + escapeHtml(center.name || "中心") + "</div>",
        anchor: "bottom-center", zIndex: 120,
      });
      map.add(cm); this._layers.push(cm); overlays.push(cm);
    }

    (geo.candidates || [])
      .filter((c) => isFinite(+c.lat) && isFinite(+(c.lon != null ? c.lon : c.lng)))
      .forEach((c) => {
        const mk = new AMap.Marker({ position: toGcjLL(c), title: c.name || "", zIndex: 100 });
        mk.on("click", () => onSelect && onSelect(c));
        map.add(mk); this._layers.push(mk); overlays.push(mk);
      });

    if (center && geo.radius_km != null && isFinite(+geo.radius_km)) {
      const circle = new AMap.Circle({
        center: toGcjLL(center), radius: (+geo.radius_km) * 1000,
        strokeColor: "#888", strokeWeight: 1.5, strokeStyle: "dashed",
        fillColor: "#4f6ef7", fillOpacity: 0.06,
      });
      map.add(circle); this._layers.push(circle); overlays.push(circle);
    }
    if (overlays.length) map.setFitView(overlays, false, [40, 40, 40, 40]);
  }

  /**
   * 路线规划（统一接口，路线由高德计算并绘制在本插件地图上）。
   * @param {{origin:{name,lat,lon|lng}, destination:{name,lat,lon|lng}, mode:string}} opts
   * @returns {Promise<RouteResult>} {provider, mode, ok, distanceMeters, durationSeconds, summary, steps?, raw}
   */
  planRoute({ origin, destination, mode } = {}) {
    const AMap = this._AMap, map = this._map;
    return new Promise((resolve) => {
      const fail = (msg) => resolve({ provider: "amap", mode, ok: false, error: msg || "暂时无法获取路线。" });
      if (!map) return fail("地图未挂载");
      this._clearRoute();
      const p1 = wgs84ToGcj02(+(origin.lon != null ? origin.lon : origin.lng), +origin.lat);
      const p2 = wgs84ToGcj02(+(destination.lon != null ? destination.lon : destination.lng), +destination.lat);
      const o = new AMap.LngLat(p1.lng, p1.lat);
      const d = new AMap.LngLat(p2.lng, p2.lat);
      const city = this._cfg.city || "北京";

      this._routeLayers = [
        new AMap.Marker({ position: o, content: '<div style="transform:translate(-50%,-100%);color:#fff;background:#1a9e6b;font-weight:700;padding:2px 8px;border-radius:8px;white-space:nowrap;">起 ' + escapeHtml(origin.name || "起点") + "</div>", anchor: "bottom-center", zIndex: 200 }),
        new AMap.Marker({ position: d, content: '<div style="transform:translate(-50%,-100%);color:#fff;background:#e5484d;font-weight:700;padding:2px 8px;border-radius:8px;white-space:nowrap;">终 ' + escapeHtml(destination.name || "终点") + "</div>", anchor: "bottom-center", zIndex: 200 }),
      ];
      map.add(this._routeLayers);

      const common = { map, autoFitView: true, hideMarkers: true };
      let planner;
      try {
        if (mode === "driving") planner = new AMap.Driving({ ...common });
        else if (mode === "walking") planner = new AMap.Walking({ ...common });
        else if (mode === "cycling") planner = new AMap.Riding({ ...common });
        else if (mode === "transit") planner = new AMap.Transfer({ ...common, city, policy: AMap.TransferPolicy.LEAST_TIME });
        else return fail("不支持的出行方式。");
      } catch (e) {
        return fail("该出行方式在当前地图服务不可用。");
      }
      this._planner = planner;

      const finish = (status, result) => {
        if (status !== "complete" || !result) return fail("暂时无法获取路线。");
        try {
          const parsed = parseRouteResult(mode, result);
          if (!parsed) return fail("未找到可用路线。");
          const { distanceMeters, durationSeconds, steps } = parsed;
          const parts = [];
          if (isFinite(distanceMeters)) parts.push(formatKm(distanceMeters));
          if (isFinite(durationSeconds)) parts.push("约 " + formatMin(durationSeconds));
          resolve({
            provider: "amap", mode, ok: true,
            distanceMeters: isFinite(distanceMeters) ? distanceMeters : null,
            durationSeconds: isFinite(durationSeconds) ? durationSeconds : null,
            summary: parts.join(" · ") || "路线已规划（距离/时间未知）",
            steps, raw: { distance: distanceMeters, time: durationSeconds },
          });
        } catch (e) {
          fail("路线结果解析失败。");
        }
      };

      try {
        if (mode === "transit") planner.search(o, d, { cityName: city }, finish);
        else planner.search(o, d, finish);
      } catch (e) {
        fail("路线请求异常。");
      }
    });
  }

  exitRoute(silent) {
    this._clearRoute();
    if (!silent && this._map && this._layers.length) {
      try { this._map.setFitView(this._layers, false, [40, 40, 40, 40]); } catch { /* ignore */ }
    }
  }

  _clearRoute() {
    const map = this._map;
    if (!map) return;
    if (this._routeLayers) { try { map.remove(this._routeLayers); } catch { /* ignore */ } this._routeLayers = null; }
    if (this._planner) { try { this._planner.clear(); } catch { /* ignore */ } this._planner = null; }
  }
  _clearLayers() {
    if (this._map && this._layers.length) { try { this._map.remove(this._layers); } catch { /* ignore */ } this._layers = []; }
  }

  /** 外部打开地点（输入 WGS84，内部转 GCJ-02）。 */
  openExternalSite(p) {
    const lon = p.lng != null ? +p.lng : +p.lon;
    const g = wgs84ToGcj02(lon, +p.lat);
    return buildExternalSiteUrl({ name: p.name, lat: g.lat, lng: g.lng });
  }
  /** 外部打开路线（输入 WGS84）。 */
  openExternalRoute({ origin, destination, mode } = {}) {
    const gf = wgs84ToGcj02(+(origin.lng != null ? origin.lng : origin.lon), +origin.lat);
    const gt = wgs84ToGcj02(+(destination.lng != null ? destination.lng : destination.lon), +destination.lat);
    return buildExternalRouteUrl(
      { name: origin.name, lat: gf.lat, lng: gf.lng },
      { name: destination.name, lat: gt.lat, lng: gt.lng },
      mode,
    );
  }

  destroy() {
    try { this._clearRoute(); this._clearLayers(); this._map && this._map.destroy(); } catch { /* ignore */ }
    this._map = null;
  }
}

/**
 * 解析高德路线结果为统一字段。兼容：
 *  - 驾车/步行/骑行：result.routes[0]（distance/time 或 duration）
 *  - 公交 Transfer：result.plans[0]（新版结构，distance 单位米、time 单位秒）
 *    以及旧版 result.routes[0].transits[0] 结构。
 * 字段缺失时返回可用部分（null），不因可选换乘详情缺失判失败。
 */
export function parseRouteResult(mode, result) {
  const num = (v) => {
    const n = Number(v);
    return isFinite(n) ? n : null;
  };
  let route = null;
  if (mode === "transit") {
    const plan = (result.plans && result.plans[0])
      || (result.routes && result.routes[0] && result.routes[0].transits && result.routes[0].transits[0]);
    if (!plan) return null;
    const distanceMeters = num(plan.distance);
    const durationSeconds = num(plan.time != null ? plan.time : plan.duration);
    let steps = [];
    const segments = plan.segments || [];
    if (Array.isArray(segments) && segments.length) {
      steps = segments.map((s) => {
        const parts = [];
        if (s.walking && s.walking.distance) parts.push(`步行 ${formatKm(s.walking.distance)}`);
        const on = s.on_station || (s.transit && s.transit.on_station);
        const off = s.off_station || (s.transit && s.transit.off_station);
        if (on && off) parts.push(`${on.name || ""} → ${off.name || ""}`);
        else if (s.transit && s.transit.name) parts.push(s.transit.name);
        return parts.join("，");
      }).filter(Boolean);
    }
    return { distanceMeters, durationSeconds, steps };
  }
  route = result.routes && result.routes[0];
  if (!route) return null;
  return {
    distanceMeters: num(route.distance),
    durationSeconds: num(route.time != null ? route.time : route.duration),
    steps: [],
  };
}

// ============ 插件定义（注册表消费）============

export const amapDefinition = {
  id: "amap",
  name: "高德地图",
  version: "1.0.0",
  builtin: true,
  available: true, // 实例级可用性由 create(config).available 决定
  capabilities: {
    map: true, markers: true, circle: true, route: true,
    routeModes: ["driving", "walking", "transit", "cycling"],
    externalSite: true, externalRoute: true,
  },
  configSchema: {
    jsKey: { label: "JS API Key", type: "string", required: true, env: "AMAP_JS_KEY", public: true },
    securityCode: { label: "Security Code", type: "string", required: false, env: "AMAP_SECURITY_CODE", public: true },
    city: { label: "默认城市（公交）", type: "string", required: false, env: "AMAP_CITY", public: true },
  },
  describeConfig(cfg = {}) {
    return {
      jsKeyConfigured: !!cfg.js_key_configured,
      securityConfigured: !!cfg.security_configured,
      city: cfg.city || "",
    };
  },
  create(config) { return new AMapPluginInstance(config); },
};
