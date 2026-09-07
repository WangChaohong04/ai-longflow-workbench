from longflow.llm import OpenAICompatibleDriver
from longflow.config import load_scenario
from longflow.api import AppState

def test_api_driver_preserves_location_without_model_call():
    driver = OpenAICompatibleDriver("http://unused", "test", "test")
    scenario = load_scenario("geo_site")
    goal = "在中关村附近2公里找咖啡馆，按距离从近到远排列，并说明距离类型和数据来源。"
    slots = driver.extract_slots(goal, scenario["slots"])
    assert slots["location"] == "中关村"
    assert slots["radius_km"] == 2
    assert slots["category"] == "咖啡馆"
    assert driver.plan(goal, scenario, slots)["missing_slots"] == []

def test_api_goal_does_not_ask_for_supplied_location(workdir):
    from longflow.config import load_config
    cfg = load_config()
    cfg["db_path"] = str(workdir / "test.db")
    cfg["llm"] = {"driver": "local"}
    state = AppState(cfg)
    engine = state.engine()
    engine.driver = OpenAICompatibleDriver("http://unused", "test", "test")
    result = engine.create_goal("在中关村附近2公里找咖啡馆，按距离从近到远排列，并说明距离类型和数据来源。", "geo_site")
    assert result["verdict"] == "pass"
    state.conn.close()
