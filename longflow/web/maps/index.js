/* Map Plugin 注册表 —— 工作台不绑定任何具体地图 Provider。
 *
 * 上层（MapPanel / 地理分析卡片 / Agent 结果展示）只与本注册表和统一插件实例交互，
 * 不允许直接出现 AMap / Google / uri.amap.com 等 provider 专属逻辑。
 *
 * 统一插件实例接口（轻量，无继承体系）：
 *   mount(container, options)              -> Promise   挂载地图
 *   showSites(geoResult, onSelect)         -> 普通模式：中心/候选 Marker + 半径 Circle
 *   planRoute({ origin, destination, mode }) -> Promise<RouteResult>  路线规划+绘制（插件自己画）
 *   exitRoute()                            -> 退出路线回普通模式
 *   openExternalSite(site)                 -> URL（在外部地图 App/网页打开地点）
 *   openExternalRoute({origin,destination,mode}) -> URL（外部地图路线）
 *   getDisplayName()                       -> string（"高德地图" / "Google Maps" / ...）
 *   getExternalOpenLabel()                 -> string（"在高德地图打开" / ...）
 *   destroy()                              -> 清理地图实例/监听/路线/DOM
 *
 * 能力通过实例 capabilities 声明，UI 据此显示/隐藏功能：
 *   { map, markers, circle, route, routeModes: ["driving","walking","transit","cycling"],
 *     externalSite, externalRoute }
 *
 * 统一路线模式（上层标准，provider 差异由插件内部映射）：
 *   driving | walking | transit | cycling
 *   （高德内部：transit→AMap.Transfer，cycling→AMap.Riding；Google 未来 cycling→bicycling）
 */

import { amapDefinition } from "./amap.js";
import { googleMapDefinition } from "./google.js";

// 统一路线模式与中文标签（上层 UI 标准）
export const MAP_ROUTE_MODES = [
  { id: "driving", label: "驾车" },
  { id: "walking", label: "步行" },
  { id: "transit", label: "公交" },
  { id: "cycling", label: "骑行" },
];

const _registry = new Map(); // id -> definition

// 用户在插件页手动选择的地图插件（localStorage；须在使用前声明，避免 TDZ）。
const MANUAL_KEY = "longflow.mapPlugin";

/** 注册一个地图插件（内置或第三方）。definition 见文件头注释。 */
export function registerMapPlugin(definition) {
  if (!definition || !definition.id || typeof definition.create !== "function") {
    throw new Error("registerMapPlugin: 需要 { id, create(config) }");
  }
  _registry.set(definition.id, definition);
}

export function getMapPluginDefinition(id) {
  return _registry.get(id) || null;
}

/** 反注册地图插件（测试/热卸载第三方插件用）。 */
export function unregisterMapPlugin(id) {
  return _registry.delete(id);
}

/** 实例是否可用：definition 未明确 available:false 且 create() 成功返回实例时默认可用；
 * 实例可在运行时额外声明 available:false（如缺少 AMap JS Key）。 */
function _instanceAvailable(def, pluginCfg) {
  if (!def || def.available === false || def.coming_soon) return false;
  try {
    const inst = def.create(pluginCfg || {});
    if (!inst) return false;
    return inst.available !== false; // undefined => 默认可用
  } catch { return false; }
}

/** 列出所有已注册地图插件的元数据（含是否可用、能力、配置状态）。 */
export function listMapPlugins(publicMapConfig = {}) {
  const cfg = publicMapConfig || {};
  return [..._registry.values()].map((def) => {
    const pluginCfg = cfg[def.id] || {};
    return {
      id: def.id,
      name: def.name,
      version: def.version || "",
      builtin: !!def.builtin,
      available: _instanceAvailable(def, pluginCfg),
      coming_soon: !!def.coming_soon,
      active: false, // 由调用方按当前选择填充
      capabilities: def.capabilities || {},
      configSchema: def.configSchema || {},
      configStatus: def.describeConfig ? def.describeConfig(pluginCfg) : {},
    };
  });
}

/** 当前选中的插件 id（优先级：用户手动选择 > 服务端配置 active > 第一个可用 > null）。 */
export function getActiveMapPluginId(publicMapConfig = {}) {
  const cfg = publicMapConfig || {};
  let manual = null;
  try { manual = localStorage.getItem(MANUAL_KEY); } catch { /* ignore */ }
  const order = [manual, cfg.active, ..._registry.keys()].filter(Boolean);
  for (const id of order) {
    if (_instanceAvailable(_registry.get(id), cfg[id] || {})) return id;
  }
  return null;
}

/** 获取当前启用插件的实例；无可用插件返回 null（上层回退内联 SVG）。 */
export function getActiveMapPlugin(publicMapConfig = {}) {
  const cfg = publicMapConfig || {};
  const id = getActiveMapPluginId(cfg);
  if (!id) return null;
  const def = _registry.get(id);
  try {
    const inst = def.create(cfg[id] || {});
    return inst && inst.available !== false ? inst : null;
  } catch { return null; }
}

export function setActiveMapPlugin(id) {
  try {
    if (id) localStorage.setItem(MANUAL_KEY, id);
    else localStorage.removeItem(MANUAL_KEY);
  } catch { /* ignore */ }
}

export function getManualMapPlugin() {
  try { return localStorage.getItem(MANUAL_KEY); } catch { return null; }
}

// 兼容旧名称（内部已全部切换，保留导出避免外部引用报错）
export const setManualMapPlugin = setActiveMapPlugin;

// ---- 注册内置插件 ----
registerMapPlugin(amapDefinition);
registerMapPlugin(googleMapDefinition);
