"""插件可通过 get_domains() 注册领域包（核心不写死领域）。"""
from __future__ import annotations

from longflow import domains as dom_mod
from longflow.plugins import loader
from longflow.plugins.sdk import Plugin


class _DomainPlugin(Plugin):
    name = "domain_demo"
    version = "1.0"
    def get_tools(self):
        return []
    def get_domains(self):
        return [{
            "id": "book_research",
            "name": "书籍研究",
            "description": "选书、版本与书评研究",
            "trigger_keywords": ["买书", "选书", "书评", "书籍"],
            "semantic_hints": ["阅读", "作者", "版本", "出版社"],
            "slots": [{"name": "topic", "type": "text", "prompt": "主题",
                       "required_for": ["*"]}],
            "subagents": ["web_researcher", "normalizer", "evidence_verifier"],
            "risk": "low",
        }]


class _BadDomainPlugin(Plugin):
    name = "domain_bad"
    version = "1.0"
    def get_tools(self):
        return []
    def get_domains(self):
        # 引用不存在的固定 subagent -> 注册时应被拒绝（不能扩大固定能力）
        return [{"id": "x", "name": "X", "description": "x",
                 "subagents": ["not_a_real_subagent"]}]


def test_collect_domains_from_plugin():
    lp = loader.LoadedPlugin(manifest={"name": "domain_demo"}, path=__import__("pathlib").Path("."),
                             instance=_DomainPlugin())
    packs = loader.collect_domains([lp])
    assert len(packs) == 1
    reg = dom_mod.load_default_registry(plugin_domains=[packs[0][0]])
    got = reg.get("book_research")
    assert got is not None and got.name == "书籍研究"
    # 路由器能识别该插件领域（非纯关键词：语义提示也参与）
    scored = reg.score("想找某作者的书，看看书评")
    top = max(scored, key=lambda x: x[1])
    assert top[0].id == "book_research"


def test_plugin_domain_with_unknown_subagent_rejected():
    import pytest
    lp = loader.LoadedPlugin(manifest={"name": "domain_bad"}, path=__import__("pathlib").Path("."),
                             instance=_BadDomainPlugin())
    packs = loader.collect_domains([lp])
    reg = dom_mod.DomainRegistry()
    with pytest.raises(ValueError):
        reg.register(dom_mod.pack_from_dict(packs[0][0]))
