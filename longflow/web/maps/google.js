/* Google Maps 地图插件 —— 第一版仅占位（Coming Soon）。
 * 未来实现时提供与 AMapPlugin 相同的方法：
 *   mount(host) / showSites(geo, onSelect) / planRoute(from,to,mode,cb)
 *   exitRoute() / openExternalSite(p) / openExternalRoute(f,t,mode)
 * 上层（getActiveMapPlugin / 地理卡片）无需改动即可切换。
 */
export class GoogleMapsPlugin {
  constructor(cfg = {}) {
    this.id = "google";
    this.label = "Google Maps";
    this.coming_soon = true;
    this.available = false; // 未配置：注册表会跳过本插件
    this.reasonUnavailable = "Google Maps 插件尚未配置（Coming Soon）。";
    this.modes = [
      { id: "driving", label: "驾车" },
      { id: "walking", label: "步行" },
      { id: "transfer", label: "公交" },
      { id: "riding", label: "骑行" },
    ];
  }

  async mount() { throw new Error(this.reasonUnavailable); }
  showSites() { throw new Error(this.reasonUnavailable); }
  planRoute(_f, _t, _m, cb) { cb && cb({ ok: false, error: this.reasonUnavailable }); }
  exitRoute() {}
  openExternalSite(p) {
    const lng = p.lng != null ? p.lng : p.lon;
    return `https://www.google.com/maps/search/?api=1&query=${+p.lat},${+lng}`;
  }
  openExternalRoute(f, t) {
    const flng = f.lng != null ? f.lng : f.lon;
    const tlng = t.lng != null ? t.lng : t.lon;
    return `https://www.google.com/maps/dir/?api=1&origin=${+f.lat},${+flng}&destination=${+t.lat},${+tlng}`;
  }
}
