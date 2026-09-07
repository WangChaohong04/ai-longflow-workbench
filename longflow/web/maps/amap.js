/* 高德地图插件（AMapPlugin）—— 第一版默认地图插件。
 *
 * 职责边界（不过度工程化）：
 *   Agent/工作台：搜索、筛选、Haversine 直线距离、分析结论
 *   高德地图：真实地图底图、Marker、道路路线计算与绘制、外部导航
 * 本文件不实现任何道路图/寻路/Polyline 算法，全部交给 AMap JS API。
 */

// ---- 纯函数：高德跳转链接 / WGS84→GCJ02 坐标转换（无浏览器依赖，可单测）----

/** 打开高德"地点"链接（POI 位置）。 */
export function buildExternalSiteUrl(p) {
  const ll = p.lng != null ? p.lng : p.lon;
  const name = encodeURIComponent(p.name || "地点");
  const lat = +p.lat, lon = +ll;
  // 网页版地点页；在手机浏览器会优先唤起高德 App。
  return `https://uri.amap.com/marker?position=${lon},${lat}&name=${name}&src=longflow&coordinate=gaode&callnative=1`;
}

/** 打开高德"路线规划"链接（真实导航交给高德）。 */
export function buildExternalRouteUrl(f, t, mode = "driving") {
  const flng = f.lng != null ? f.lng : f.lon;
  const tlng = t.lng != null ? t.lng : t.lon;
  const type = { driving: "car", walking: "walk", transfer: "bus", riding: "ride" }[mode] || "car";
  const q = new URLSearchParams({
    src: "longflow",
    coordinate: "gaode",
    callnative: "1",
    from: `${+flng},${+f.lat},${encodeURIComponent(f.name || "起点")}`,
    to: `${+tlng},${+t.lat},${encodeURIComponent(t.name || "终点")}`,
    mode: "route",
    type,
  });
  return `https://uri.amap.com/navigation?${q.toString()}`;
}

const PI = Math.PI;
const X_PI = (PI * 3000.0) / 180.0;
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

/** WGS84(GPS/GeoJSON, EPSG:4326) → GCJ-02（火星坐标，高德）。境外坐标不转换。 */
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

const MODES = [
  { id: "driving", label: "驾车" },
  { id: "walking", label: "步行" },
  { id: "transfer", label: "公交" },
  { id: "riding", label: "骑行" },
];

let _loadingPromise = null;
function loadAMap(jsKey, securityCode) {
  if (window.AMap) return Promise.resolve(window.AMap);
  if (_loadingPromise) return _loadingPromise;
  _loadingPromise = new Promise((resolve, reject) => {
    if (!jsKey) { reject(new Error("AMAP_JS_KEY 未配置")); return; }
    // 安全密钥（新版 JS API 要求）
    if (securityCode) window._AMapSecurityConfig = { securityJsCode: securityCode };
    const cb = "__amap_init__" + Math.random().toString(36).slice(2);
    window[cb] = () => { resolve(window.AMap); try { delete window[cb]; } catch { window[cb] = undefined; } };
    const s = document.createElement("script");
    s.src = `https://webapi.amap.com/maps?v=2.0&key=${encodeURIComponent(jsKey)}&plugin=AMap.Driving,AMap.Walking,AMap.Transfer,AMap.Riding&callback=${cb}`;
    s.async = true;
    s.onerror = () => reject(new Error("高德地图 SDK 加载失败（网络异常或 Key 无效）"));
    document.head.appendChild(s);
    setTimeout(() => { if (!window.AMap) reject(new Error("高德地图 SDK 加载超时")); }, 15000);
  }).catch((e) => { _loadingPromise = null; throw e; });
  return _loadingPromise;
}

export class AMapPlugin {
  constructor(cfg = {}) {
    this.id = "amap";
    this.label = "高德地图";
    this.available = cfg.enabled !== false && !!cfg.js_key;
    this.reasonUnavailable = !cfg.js_key ? "未配置 AMAP_JS_KEY" : (cfg.enabled === false ? "插件已禁用" : "");
    this.modes = MODES;
    this._cfg = cfg;
    this._map = null;
    this._layers = []; // 普通模式覆盖物（marker/circle）
    this._info = null;
    this._mode = "normal"; // normal | route
  }

  // ---------- 挂载 / 普通模式 ----------
  async mount(host) {
    if (this._map) return;
    const AMap = await loadAMap(this._cfg.js_key, this._cfg.security_code);
    this._AMap = AMap;
    this._map = new AMap.Map(host, { zoom: 13, viewMode: "2D" });
  }

  /** 普通模式：中心 marker + 候选 markers + 半径 circle，自动调整视野。 */
  showSites(geo, onSelect) {
    const AMap = this._AMap, map = this._map;
    if (!map) throw new Error("地图未挂载");
    this.exitRoute(true);
    this._clearLayers();
    const overlays = [];
    const toGcj = (c) => {
      const lon = c.lon != null ? +c.lon : +c.lng;
      const g = wgs84ToGcj02(lon, +c.lat);
      return new AMap.LngLat(g.lng, g.lat);
    };
    const center = geo.center && isFinite(+geo.center.lat) ? geo.center : null;

    if (center) {
      const cpos = toGcj(center);
      const cm = new AMap.Marker({
        position: cpos,
        content: '<div style="transform:translate(-50%,-100%);color:#fff;background:#e5484d;font-weight:700;padding:2px 7px;border-radius:8px;white-space:nowrap;box-shadow:0 1px 4px rgba(0,0,0,.3);">★ ' + (center.name || "中心") + "</div>",
        anchor: "bottom-center", zIndex: 120,
      });
      map.add(cm); this._layers.push(cm); overlays.push(cpos);
    }

    const cands = (geo.candidates || []).filter((c) => isFinite(+c.lat) && isFinite(+(c.lon != null ? c.lon : c.lng)));
    cands.forEach((c, i) => {
      const pos = toGcj(c);
      const mk = new AMap.Marker({ position: pos, title: c.name || "", zIndex: 100 });
      mk.on("click", () => onSelect && onSelect(c));
      map.add(mk); this._layers.push(mk); overlays.push(pos);
    });

    if (center && geo.radius_km != null && isFinite(+geo.radius_km)) {
      const circle = new AMap.Circle({
        center: toGcj(center),
        radius: (+geo.radius_km) * 1000,
        strokeColor: "#888", strokeWeight: 1.5, strokeStyle: "dashed",
        fillColor: "#4f6ef7", fillOpacity: 0.06,
      });
      map.add(circle); this._layers.push(circle);
    }

    if (overlays.length) map.setFitView(overlays, false, [40, 40, 40, 40]);
  }

  // ---------- 路线模式 ----------
  /**
   * @param from/to {name,lat,lon}   WGS84 坐标
   * @param mode driving|walking|transfer|riding
   * @param cb ({ok, mode, distanceText, durationText, segments, error}) => void
   */
  planRoute(from, to, mode, cb) {
    const AMap = this._AMap, map = this._map;
    if (!map) { cb && cb({ ok: false, error: "地图未挂载" }); return; }
    this._clearRoute();
    this._mode = "route";
    const p1 = wgs84ToGcj02(+from.lon, +from.lat);
    const p2 = wgs84ToGcj02(+to.lon, +to.lat);
    const origin = new AMap.LngLat(p1.lng, p1.lat);
    const dest = new AMap.LngLat(p2.lng, p2.lat);
    const city = this._cfg.city || "北京";

    const common = { map, autoFitView: true, hideMarkers: true };
    let planner;
    const finish = (status, result) => {
      if (status !== "complete" || !result) {
        cb && cb({ ok: false, mode, error: "暂时无法获取高德路线。" });
        return;
      }
      try {
        const route = result.routes && result.routes[0];
        if (!route) { cb && cb({ ok: false, mode, error: "未找到可用路线。" }); return; }
        const distanceText = formatKm(route.distance);
        const durationText = formatMin(route.time != null ? route.time : route.duration);
        let segments = [];
        if (mode === "transfer" && route.transits && route.transits[0]) {
          const tr = route.transits[0];
          segments = (tr.segments || []).map((s) => {
            const parts = [];
            if (s.walking && s.walking.distance) parts.push(`步行 ${formatKm(s.walking.distance)}`);
            if (s.on_station && s.off_station) parts.push(`${s.on_station.name || ""} → ${s.off_station.name || ""}`);
            return parts.join("，");
          }).filter(Boolean);
        }
        cb && cb({ ok: true, mode, distanceText, durationText, segments });
      } catch (e) {
        cb && cb({ ok: false, mode, error: "路线结果解析失败。" });
      }
    };

    // 起终点标记
    this._routeLayers = [
      new AMap.Marker({ position: origin, content: '<div style="transform:translate(-50%,-100%);color:#fff;background:#1a9e6b;font-weight:700;padding:2px 8px;border-radius:8px;white-space:nowrap;">起 ' + (from.name || "起点") + "</div>", anchor: "bottom-center", zIndex: 200 }),
      new AMap.Marker({ position: dest, content: '<div style="transform:translate(-50%,-100%);color:#fff;background:#e5484d;font-weight:700;padding:2px 8px;border-radius:8px;white-space:nowrap;">终 ' + (to.name || "终点") + "</div>", anchor: "bottom-center", zIndex: 200 }),
    ];
    map.add(this._routeLayers);

    try {
      if (mode === "driving") planner = new AMap.Driving({ ...common });
      else if (mode === "walking") planner = new AMap.Walking({ ...common });
      else if (mode === "riding") planner = new AMap.Riding({ ...common });
      else if (mode === "transfer") planner = new AMap.Transfer({ ...common, city, policy: AMap.TransferPolicy.LEAST_TIME });
      else { cb && cb({ ok: false, mode, error: "不支持的出行方式。" }); return; }
    } catch (e) {
      cb && cb({ ok: false, mode, error: "该出行方式在当前地图服务不可用。" });
      return;
    }
    this._planner = planner;
    try {
      if (mode === "transfer") planner.search(origin, dest, { cityName: city }, finish);
      else planner.search(origin, dest, finish);
    } catch (e) {
      cb && cb({ ok: false, mode, error: "路线请求异常。" });
    }
  }

  exitRoute(silent) {
    this._mode = "normal";
    this._clearRoute();
    if (!silent) this._map && this._map.setFitView(this._layers, false, [40, 40, 40, 40]);
  }

  _clearRoute() {
    const map = this._map, AMap = this._AMap;
    if (!map) return;
    if (this._routeLayers) { map.remove(this._routeLayers); this._routeLayers = null; }
    if (this._planner) { try { this._planner.clear(); } catch { /* ignore */ } this._planner = null; }
  }

  _clearLayers() {
    const map = this._map;
    if (!map) return;
    if (this._layers.length) { map.remove(this._layers); this._layers = []; }
  }

  destroy() {
    try { this._clearRoute(); this._clearLayers(); this._map && this._map.destroy(); } catch { /* ignore */ }
    this._map = null;
  }

  openExternalSite(p) { return amapSiteUrl(p); }
  openExternalRoute(f, t, mode) { return amapRouteUrl(f, t, mode); }
}

/** 跳转高德地点（输入 WGS84，内部转 GCJ-02）。地图未挂载时也可用（走网页/App）。 */
export function amapSiteUrl(p) {
  const lon = p.lng != null ? +p.lng : +p.lon;
  const g = wgs84ToGcj02(lon, +p.lat);
  return buildExternalSiteUrl({ name: p.name, lat: g.lat, lng: g.lng });
}

/** 跳转高德路线规划（输入 WGS84，内部转 GCJ-02）。 */
export function amapRouteUrl(f, t, mode) {
  const gf = wgs84ToGcj02(+(f.lng != null ? f.lng : f.lon), +f.lat);
  const gt = wgs84ToGcj02(+(t.lng != null ? t.lng : t.lon), +t.lat);
  return buildExternalRouteUrl(
    { name: f.name, lat: gf.lat, lng: gf.lng },
    { name: t.name, lat: gt.lat, lng: gt.lng },
    mode,
  );
}

function formatKm(meters) {
  const m = Number(meters) || 0;
  return m >= 1000 ? `${(m / 1000).toFixed(1)} km` : `${Math.round(m)} m`;
}
function formatMin(seconds) {
  const s = Number(seconds) || 0;
  return `${Math.max(1, Math.round(s / 60))} min`;
}
