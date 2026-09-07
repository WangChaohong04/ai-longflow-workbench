"""命令行入口：python -m longflow.cli serve / init-db / reset / tick。"""
from __future__ import annotations

import argparse

import uvicorn

from . import config as cfg_mod
from . import db
from .api import create_app


def main() -> None:
    parser = argparse.ArgumentParser(prog="longflow")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("init-db", help="初始化数据库")
    sub.add_parser("reset", help="删除数据库")
    p_serve = sub.add_parser("serve", help="启动 HTTP 服务与工作台")
    p_serve.add_argument("--host", default=None)
    p_serve.add_argument("--port", type=int, default=None)
    p_tick = sub.add_parser("tick", help="对所有未完成任务执行一次恢复推进")
    p_tick.add_argument("--root", default=None, help="指定 root 任务 id")
    args = parser.parse_args()

    cfg = cfg_mod.load_config()

    if args.cmd == "init-db":
        conn = db.connect(cfg["db_path"])
        db.init_db(conn)
        print(f"数据库已初始化: {cfg['db_path']}")
    elif args.cmd == "reset":
        db.reset_db(cfg["db_path"])
        print(f"数据库已删除: {cfg['db_path']}")
    elif args.cmd == "tick":
        from .api import AppState

        state = AppState(cfg)
        engine = state.engine()
        if args.root:
            print(engine.tick(args.root))
        else:
            rows = state.conn.execute(
                "SELECT DISTINCT root_id FROM tasks WHERE status NOT IN ('completed','failed','cancelled')"
            ).fetchall()
            for r in rows:
                print(r["root_id"], engine.tick(r["root_id"]))
    elif args.cmd == "serve":
        host = args.host or cfg["server"]["host"]
        port = args.port or cfg["server"]["port"]
        app = create_app(cfg)
        print(f"LongFlow 工作台: http://{host}:{port}")
        uvicorn.run(app, host=host, port=port)


if __name__ == "__main__":
    main()
