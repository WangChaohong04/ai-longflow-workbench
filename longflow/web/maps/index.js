/* 地图插件注册表（轻量 MapPlugin 接口，不引入复杂抽象层）。
 *
 * 上层（地理分析卡片 / 工作台）只与本注册表交互：
 *   const plugin = getActiveMapPlugin();  // 当前启用的地图插件
 *   await plugin.mount(host, ctx);        // 挂载地图
 *   plugin.showSites(geo);                // 普通模式：中心+候选+半径圆
 *   plugin.planRoute(from, to, mode, cb); // 路线模式：交给地图服务算路
 *   plugin.exitRoute();                   // 退出路线回到普通模式
 *   plugin.externalUrl(...)               // 跳转外部地图（App/网页）
 *
 * 第一版实现：AMapPlugin（默认开启）。
 * 预留：GoogleMapsPlugin（Coming Soon，未配置）。
 * 选择优先级：用户手动选择 > 系统配置 > 默认（amap）。
 */

import { AMapPlugin } from "./amap.js";
import { GoogleMapsPlugin } from "./google.js";

const REGISTRY = {
  amap: AMapPlugin,
  google: GoogleMapsPlugin,
};

// 用户在插件页手动选择的地图插件（localStorage，演示/测试可覆盖）。
const MANUAL_KEY = "longflow.mapPlugin";

export function listMapPlugins(publicMapConfig) {
  const cfg = publicMapConfig || {};
  return [
    {
      id: "amap",
      label: "高德地图 AMap",
      desc: "中国大陆地图、地点与路线服务；负责真实地图展示、道路路线与导航跳转。",
      enabled: (cfg.amap && cfg.amap.enabled) !== false,
      configured: !!(cfg.amap && cfg.amap.js_key_configured),
      coming_soon: false,
    },
    {
      id: "google",
      label: "Google Maps",
      desc: "国际地图与路线服务。",
      enabled: !!(cfg.google && cfg.google.enabled),
      configured: false,
      coming_soon: true,
    },
  ];
}

export function getActiveMapPlugin(publicMapConfig) {
  const cfg = publicMapConfig || {};
  let chosen = null;
  try { chosen = localStorage.getItem(MANUAL_KEY); } catch { /* ignore */ }
  const order = [chosen, cfg.active, "amap"].filter(Boolean);
  for (const id of order) {
    const Cls = REGISTRY[id];
    if (!Cls) continue;
    const plugin = new Cls(cfg[id] || {});
    if (plugin.available) return plugin;  // coming_soon / 未启用 → 继续尝试下一个
  }
  return null; // 无可用地图插件：上层回退到内联 SVG，不崩溃
}

export function setManualMapPlugin(id) {
  try {
    if (id) localStorage.setItem(MANUAL_KEY, id);
    else localStorage.removeItem(MANUAL_KEY);
  } catch { /* ignore */ }
}

export function getManualMapPlugin() {
  try { return localStorage.getItem(MANUAL_KEY); } catch { return null; }
}
