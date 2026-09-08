/* Google Maps 地图插件 —— 第一版仅占位（Coming Soon，available=false）。
 * 未来实现时提供与 AMap 插件相同的统一接口（mount/showSites/planRoute/
 * exitRoute/openExternalSite/openExternalRoute/getDisplayName/getExternalOpenLabel/destroy），
 * WGS84 坐标无需转换，cycling 内部映射为 Google bicycling。上层无需改动。
 */

class GoogleMapsPluginInstance {
  constructor(cfg = {}) {
    this.id = "google";
    this._cfg = cfg;
    this.available = false; // 未配置：注册表跳过本插件
    this.reasonUnavailable = "Google Maps 插件尚未配置（Coming Soon）。";
    this.capabilities = {
      map: false, markers: false, circle: false, route: false,
      routeModes: ["driving", "walking", "transit", "cycling"],
      externalSite: true, externalRoute: true,
    };
  }
  getDisplayName() { return "Google Maps"; }
  getExternalOpenLabel() { return "在 Google Maps 打开"; }

  async mount() { throw new Error(this.reasonUnavailable); }
  showSites() { throw new Error(this.reasonUnavailable); }
  async planRoute() { return { provider: "google", ok: false, error: this.reasonUnavailable }; }
  exitRoute() {}

  openExternalSite(p) {
    const lng = p.lng != null ? p.lng : p.lon;
    return `https://www.google.com/maps/search/?api=1&query=${+p.lat},${+lng}`;
  }
  openExternalRoute({ origin, destination } = {}) {
    const olng = origin.lng != null ? origin.lng : origin.lon;
    const tlng = destination.lng != null ? destination.lng : destination.lon;
    return `https://www.google.com/maps/dir/?api=1&origin=${+origin.lat},${+olng}&destination=${+destination.lat},${+tlng}`;
  }
  destroy() {}
}

export const googleMapDefinition = {
  id: "google",
  name: "Google Maps",
  version: "0.0.0",
  builtin: true,
  coming_soon: true,
  available: false,
  capabilities: {
    map: false, markers: false, circle: false, route: false,
    routeModes: ["driving", "walking", "transit", "cycling"],
    externalSite: true, externalRoute: true,
  },
  configSchema: {
    apiKey: { label: "API Key", type: "string", required: true },
  },
  describeConfig() { return { comingSoon: true }; },
  create(config) { return new GoogleMapsPluginInstance(config); },
};
