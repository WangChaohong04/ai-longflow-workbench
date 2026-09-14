"""Batch6：知识版本/生效/失效——已失效资料不参与检索，现行版本可召回。"""
from tests.conftest import workdir  # noqa: F401

import longflow.db as db
import longflow.rag as rag
from longflow.rag import _doc_meta


def test_doc_meta_parses_dates():
    text = "# 标准（2023 版）\n- 版本：v2023.1\n- 生效日期：2023-01-01\n- 失效日期：2023-12-31\n## 第一条\n住宿 400元"
    m = _doc_meta(text, "travel_2023.md")
    assert m["version"].startswith("v2023")
    assert m["effective_at"] == "2023-01-01"
    assert m["expires_at"] == "2023-12-31"


def test_expired_knowledge_excluded_from_search(workdir):
    conn = db.connect(str(workdir / "k.db"))
    db.init_db(conn)
    rag._ensure_rag_cols(conn)
    # 旧版（已失效）与新版（有效）
    conn.execute(
        """INSERT INTO knowledge_chunks
           (id, source_id, doc_name, section, text, fields_json, citations_json,
            scenario, version, effective_at, expires_at)
           VALUES ('kc_old','s','travel_2023.md','住宿','北京上海住宿限额 400元','{}','[]',
                   'team_ops','2023','2023-01-01','2023-12-31')""")
    conn.execute(
        """INSERT INTO knowledge_chunks
           (id, source_id, doc_name, section, text, fields_json, citations_json,
            scenario, version, effective_at, expires_at)
           VALUES ('kc_new','s','travel_2024.md','住宿','北京上海住宿限额 500元','{}','[]',
                   'team_ops','2024','2024-01-01',NULL)""")
    conn.commit()

    # 默认（今天 as_of 2025）：旧版被排除，只召回新版
    res = rag.search(conn, "出差住宿标准限额", scenario="team_ops", top_k=5)
    docs = {r["doc_name"] for r in res}
    assert "travel_2024.md" in docs
    assert "travel_2023.md" not in docs, "已失效的 2023 版不应参与检索"
    assert all("400" not in r["text"] for r in res)

    # include_expired 可显式召回（供版本对账）
    res_all = rag.search(conn, "住宿限额", scenario="team_ops", top_k=5, include_expired=True)
    docs_all = {r["doc_name"] for r in res_all}
    assert "travel_2023.md" in docs_all


def test_not_yet_effective_excluded(workdir):
    conn = db.connect(str(workdir / "f.db"))
    db.init_db(conn)
    rag._ensure_rag_cols(conn)
    conn.execute(
        """INSERT INTO knowledge_chunks
           (id, source_id, doc_name, section, text, fields_json, citations_json,
            scenario, version, effective_at, expires_at)
           VALUES ('kc_future','s','future.md','x','未来政策 补贴 999元','{}','[]',
                   'team_ops','2099','2099-01-01',NULL)""")
    conn.commit()
    res = rag.search(conn, "补贴", scenario="team_ops")
    assert all(r["doc_name"] != "future.md" for r in res), "尚未生效的知识不应召回"
