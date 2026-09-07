"""示例天气插件。

用途：证明"不修改 LongFlow 核心即可新增工具"（SPEC §8）。
数据为内置固定样例，**不是真实天气预报**：每条结果都显式标注 mock: true 与
source: 'mock'；未知城市返回 found:false，不编造天气。
"""
from __future__ import annotations

from longflow.plugins.sdk import Plugin, PluginError, ToolContext, spec

MOCK_WEATHER: dict[str, dict] = {
    "北京": {
        "city": "北京",
        "condition": "晴",
        "temperature_c": 22,
        "humidity_pct": 45,
        "wind": "西北风3级",
    },
    "上海": {
        "city": "上海",
        "condition": "多云",
        "temperature_c": 26,
        "humidity_pct": 70,
        "wind": "东南风2级",
    },
    "深圳": {
        "city": "深圳",
        "condition": "阵雨",
        "temperature_c": 29,
        "humidity_pct": 85,
        "wind": "南风2级",
    },
}


class ExampleWeatherPlugin(Plugin):
    def get_tools(self) -> list:
        return [
            spec(
                "weather_get",
                "查询城市天气（示例工具，返回内置 mock 固定数据，非真实预报）。",
                self.t_weather_get,
                risk="low",
                side_effect=False,
                params_schema={
                    "properties": {
                        "city": {"type": "string", "required": True, "description": "城市名，如 北京"}
                    }
                },
            )
        ]

    def t_weather_get(self, args: dict, ctx: ToolContext) -> dict:
        city = args.get("city")
        if not isinstance(city, str) or not city.strip():
            raise PluginError("weather_get 缺少必填参数: city")
        key = city.strip()
        data = MOCK_WEATHER.get(key)
        if data is None:
            # 未知城市不编造天气
            return {
                "found": False,
                "city": key,
                "mock": True,
                "source": "mock",
                "reason": "mock 数据中无该城市",
            }
        result = dict(data)
        result["mock"] = True
        result["source"] = "mock"
        result["note"] = "本结果为插件内置固定样例数据，不代表真实天气"
        return result
