"""GEO 空间分析插件。

数据来源：plugins/geo/data/sample.geojson（本地样例数据，EPSG:4326）。

红线（SPEC §8/§11）：
- 坐标只来自本地 GeoJSON 地名词典，查不到一律 found:false，绝不编造坐标；
- 距离使用标准半正矢（haversine）公式真实计算球面距离；
- route 模式在没有外部路线 provider 时返回 supported:false，绝不编造路线距离/通勤时间；
- 无来源的属性（如 noise_level）在数据中标注 source:null，证据串中注明未核验，
  由出口闸门按"无证据不下断言"处理。
"""
from __future__ import annotations

import json
import math
import os
from typing import Any, Optional

from longflow.plugins.sdk import Plugin, PluginError, ToolContext, spec

EARTH_RADIUS_KM = 6371.0088  # 平均地球半径（km），标准 haversine 常数
CRS = "EPSG:4326"
LOCAL_SOURCE = "local_geojson"


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """标准半正矢公式计算两点球面距离（km）。"""
    rlat1, rlat2 = math.radians(lat1), math.radians(lat2)
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = (
        math.sin(dlat / 2.0) ** 2
        + math.cos(rlat1) * math.cos(rlat2) * math.sin(dlon / 2.0) ** 2
    )
    c = 2.0 * math.asin(min(1.0, math.sqrt(a)))
    return EARTH_RADIUS_KM * c


def _validate_latlon(point: Any, label: str) -> dict:
    if not isinstance(point, dict):
        raise PluginError(f"{label} 必须是 {{lat, lon}} 对象或地名字符串")
    lat = point.get("lat")
    lon = point.get("lon", point.get("lng", point.get("longitude")))
    if not isinstance(lat, (int, float)) or not isinstance(lon, (int, float)):
        raise PluginError(f"{label} 缺少有效的 lat/lon 数值")
    if not (-90.0 <= lat <= 90.0) or not (-180.0 <= lon <= 180.0):
        raise PluginError(f"{label} 坐标超出经纬度范围")
    return {"lat": float(lat), "lon": float(lon)}


class GeoPlugin(Plugin):
    """本地 GeoJSON 驱动的空间分析插件。"""

    def __init__(self) -> None:
        self._config: dict = {}
        self._data_dir: str = ""
        self._geojson: Optional[dict] = None

    # ---- 生命周期 ----
    def setup(self, config: dict, data_dir: str) -> None:  # type: ignore[override]
        self._config = config or {}
        self._data_dir = data_dir or ""

    # ---- 数据加载 ----
    def _data_path(self, ctx: ToolContext) -> str:
        base = getattr(ctx, "data_dir", "") or self._data_dir
        if not base:
            raise PluginError("geo 插件数据目录未知（ctx.data_dir 未注入）")
        return os.path.join(base, "data", "sample.geojson")

    def _load_features(self, ctx: ToolContext) -> list[dict]:
        """加载并缓存 GeoJSON Feature 列表。"""
        if self._geojson is None:
            path = self._data_path(ctx)
            try:
                with open(path, "r", encoding="utf-8") as f:
                    data = json.load(f)
            except FileNotFoundError:
                raise PluginError(f"geo 数据文件不存在: {path}")
            except json.JSONDecodeError as exc:
                raise PluginError(f"geo 数据文件不是合法 JSON: {exc}")
            if data.get("type") != "FeatureCollection":
                raise PluginError("geo 数据文件必须是 GeoJSON FeatureCollection")
            self._geojson = data
        return self._geojson.get("features", [])

    def _gazetteer(self, ctx: ToolContext) -> dict[str, dict]:
        """地名词典：name + aliases（小写、去空白）→ feature。"""
        idx: dict[str, dict] = {}
        for feat in self._load_features(ctx):
            props = feat.get("properties", {})
            geom = feat.get("geometry", {})
            if geom.get("type") != "Point":
                continue
            coords = geom.get("coordinates") or []
            if len(coords) < 2:
                continue
            names = []
            if props.get("name"):
                names.append(str(props["name"]))
            for alias in props.get("aliases", []) or []:
                names.append(str(alias))
            for n in names:
                idx[n.strip().lower()] = feat
        return idx

    def _geocode(self, location: Any, ctx: ToolContext) -> Optional[dict]:
        """地名 → {lat, lon, name}；输入本身是坐标对象时直接校验返回；查不到返回 None。"""
        if isinstance(location, dict):
            p = _validate_latlon(location, "location")
            return {"lat": p["lat"], "lon": p["lon"], "name": location.get("name")}
        if not isinstance(location, str) or not location.strip():
            raise PluginError("location 必须是非空地名字符串或 {lat, lon} 对象")
        feat = self._gazetteer(ctx).get(location.strip().lower())
        if feat is None:
            return None
        lon, lat = feat["geometry"]["coordinates"][0], feat["geometry"]["coordinates"][1]
        return {"lat": float(lat), "lon": float(lon), "name": feat["properties"].get("name")}

    # ---- 工具处理函数 ----
    def t_geocode(self, args: dict, ctx: ToolContext) -> dict:
        location = args.get("location")
        if location is None:
            raise PluginError("geo_geocode 缺少必填参数: location")
        resolved = self._geocode(location, ctx)
        if resolved is None:
            # 绝不编造坐标
            return {"found": False, "query": location, "source": LOCAL_SOURCE, "crs": CRS}
        return {
            "found": True,
            "query": location,
            "name": resolved.get("name"),
            "lat": resolved["lat"],
            "lon": resolved["lon"],
            "crs": CRS,
            "source": LOCAL_SOURCE,
        }

    def t_distance(self, args: dict, ctx: ToolContext) -> dict:
        a = args.get("a")
        b = args.get("b")
        if a is None or b is None:
            raise PluginError("geo_distance 缺少必填参数: a / b")
        mode = args.get("mode", "haversine")
        # 兼容 SPEC §8 中的拼写 haverside
        if mode == "haverside":
            mode = "haversine"

        if mode == "route":
            provider = (self._config or {}).get("provider", "local")
            if provider == "local" or not provider:
                # 无外部路线 provider：如实声明不支持，绝不编造路线距离/通勤时间
                return {
                    "supported": False,
                    "mode": "route",
                    "reason": "no_route_provider_configured",
                    "crs": CRS,
                }
            # 配置了外部 provider 但当前实现未接入：同样如实返回，不编造
            return {
                "supported": False,
                "mode": "route",
                "reason": "no_route_provider_configured",
                "provider": provider,
                "crs": CRS,
            }

        if mode != "haversine":
            raise PluginError(f"不支持的距离模式: {mode}（支持: haversine / route）")

        pa = self._geocode(a, ctx)
        if pa is None:
            return {"supported": False, "error": "place_not_found", "place": a,
                    "reason": f"本地地名词典中找不到地点: {a}"}
        pb = self._geocode(b, ctx)
        if pb is None:
            return {"supported": False, "error": "place_not_found", "place": b,
                    "reason": f"本地地名词典中找不到地点: {b}"}

        dist = haversine_km(pa["lat"], pa["lon"], pb["lat"], pb["lon"])
        return {
            "distance_km": round(dist, 3),
            "mode": "haversine",
            "crs": CRS,
            "source": LOCAL_SOURCE,
            "a": {"name": pa.get("name"), "lat": pa["lat"], "lon": pa["lon"]},
            "b": {"name": pb.get("name"), "lat": pb["lat"], "lon": pb["lon"]},
        }

    def t_radius_search(self, args: dict, ctx: ToolContext) -> dict:
        center = args.get("center")
        radius_km = args.get("radius_km")
        if center is None:
            raise PluginError("geo_radius_search 缺少必填参数: center")
        if not isinstance(radius_km, (int, float)) or radius_km <= 0:
            raise PluginError("geo_radius_search 参数 radius_km 必须是正数（km）")
        filters = args.get("filters") or {}
        if not isinstance(filters, dict):
            raise PluginError("filters 必须是字段等值匹配字典，如 {category: '咖啡馆'}")
        sort_by = args.get("sort_by", "distance")

        center_pt = self._geocode(center, ctx)
        if center_pt is None:
            return {"supported": False, "error": "place_not_found", "place": center,
                    "reason": f"本地地名词典中找不到中心点: {center}"}

        candidates: list[dict] = []
        for feat in self._load_features(ctx):
            props = dict(feat.get("properties", {}))
            geom = feat.get("geometry", {})
            if geom.get("type") != "Point":
                continue
            coords = geom.get("coordinates") or []
            if len(coords) < 2:
                continue
            lon, lat = float(coords[0]), float(coords[1])
            dist = haversine_km(center_pt["lat"], center_pt["lon"], lat, lon)
            if dist > float(radius_km):
                continue
            # filters：对 properties 字段做等值匹配
            if any(props.get(k) != v for k, v in filters.items()):
                continue

            evidence = [
                f"距中心 {dist:.2f}km（haversine 球面距离，EPSG:4326）",
                f"类别: {props.get('category', '未知')}",
            ]
            if props.get("address"):
                evidence.append(f"地址: {props['address']}")
            if props.get("rating") is not None:
                src = props.get("rating_source")
                tag = f"（来源: {src}）" if src else "（source:null，样例值未核验）"
                evidence.append(f"评分: {props['rating']}{tag}")
            if props.get("noise_level") is not None:
                src = props.get("noise_level_source")
                tag = f"（来源: {src}）" if src else "（source:null，无实测来源，不得据此断言安静）"
                evidence.append(f"噪声等级: {props['noise_level']}{tag}")

            candidates.append({
                "name": props.get("name"),
                "lat": lat,
                "lon": lon,
                "distance_km": round(dist, 3),
                "properties": props,
                "evidence": evidence,
                "source": LOCAL_SOURCE,
                "crs": CRS,
            })

        # 排序：distance（默认）或 properties 中数值字段
        if sort_by == "distance":
            candidates.sort(key=lambda c: c["distance_km"])
        else:
            for c in candidates:
                v = c["properties"].get(sort_by)
                if not isinstance(v, (int, float)):
                    raise PluginError(
                        f"sort_by 字段 '{sort_by}' 不是数值属性（候选 {c['name']}）"
                    )
            candidates.sort(key=lambda c: c["properties"][sort_by], reverse=True)

        return {
            "candidates": candidates,
            "center": {
                "name": center_pt.get("name"),
                "query": center if isinstance(center, str) else None,
                "lat": center_pt["lat"],
                "lon": center_pt["lon"],
            },
            "radius_km": float(radius_km),
            "filters": filters,
            "sort_by": sort_by,
            "total": len(candidates),
            "crs": CRS,
            "source": LOCAL_SOURCE,
        }

    # ---- 工具注册 ----
    def get_tools(self) -> list:
        return [
            spec(
                "geo_geocode",
                "本地地名词典地理编码：把地名（GeoJSON 中的 name/别名）解析为 WGS84 经纬度；"
                "未知地名返回 found:false，不编造坐标。",
                self.t_geocode,
                risk="low",
                side_effect=False,
                params_schema={
                    "properties": {
                        "location": {
                            "type": "string",
                            "required": True,
                            "description": "地名（字符串）或 {lat, lon} 坐标对象",
                        }
                    }
                },
            ),
            spec(
                "geo_distance",
                "计算两点距离。haversine 模式为真实半正矢球面直线距离（km）；"
                "route 模式需要外部路线 provider，未配置时返回 supported:false，"
                "不提供路线距离与通勤时间。",
                self.t_distance,
                risk="low",
                side_effect=False,
                params_schema={
                    "properties": {
                        "a": {"type": "any", "required": True,
                              "description": "起点：{lat, lon} 或地名字符串"},
                        "b": {"type": "any", "required": True,
                              "description": "终点：{lat, lon} 或地名字符串"},
                        "mode": {"type": "string",
                                 "description": "haversine（默认，直线）或 route（路线，需 provider）"},
                    }
                },
            ),
            spec(
                "geo_radius_search",
                "在本地 GeoJSON 候选点中按真实 haversine 距离做半径筛选，支持 properties "
                "等值过滤（如 category、noise_level）与按距离/数值字段（如 rating）排序；"
                "每个候选返回可核验证据串。",
                self.t_radius_search,
                risk="low",
                side_effect=False,
                params_schema={
                    "properties": {
                        "center": {"type": "any", "required": True,
                                   "description": "圆心：{lat, lon} 或地名字符串"},
                        "radius_km": {"type": "number", "required": True,
                                      "description": "半径（公里）"},
                        "filters": {"type": "object",
                                    "description": "properties 等值过滤，如 {category: '咖啡馆'}"},
                        "sort_by": {"type": "string",
                                    "description": "distance（默认）或数值属性字段名（如 rating）"},
                    }
                },
            ),
        ]
