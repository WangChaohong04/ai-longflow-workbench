"""恢复循环：进程重启后继续未完成工作。

等待采用持久化状态（waiting_approval / waiting_event + scheduled_at），
worker 只做轻量 tick，不在等待中空转调用模型——没有可推进任务时立即休眠。
"""
from __future__ import annotations

import threading
import time

from . import db, events
from .models import IN_PROGRESS, PENDING, READY, WAITING_APPROVAL, WAITING_EVENT


class RecoveryWorker:
    def __init__(self, engine_factory, interval_seconds: float = 2.0):
        self._factory = engine_factory  # () -> Engine（每次新连接）
        self._interval = interval_seconds
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="longflow-recovery", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)

    def _run(self) -> None:
        # 启动即恢复一次（进程重启场景）
        self._recover_all()
        while not self._stop.wait(self._interval):
            self._recover_all()

    def _recover_all(self) -> None:
        try:
            engine = self._factory()
            conn = engine.conn
            rows = conn.execute(
                """SELECT DISTINCT root_id FROM tasks
                   WHERE status IN (?, ?, ?, ?) OR root_id IN
                   (SELECT root_id FROM tasks WHERE status IN (?, ?, ?))""",
                (IN_PROGRESS, PENDING, READY, WAITING_EVENT,
                 IN_PROGRESS, PENDING, READY),
            ).fetchall()
            for r in rows:
                try:
                    engine.tick(r["root_id"])
                except Exception as exc:  # noqa: BLE001
                    events.emit(conn, events.ERROR, task_id=r["root_id"],
                                actor="recovery", detail={"error": str(exc)[:300]})
        except Exception:  # noqa: BLE001 - worker 永不因异常退出
            time.sleep(1)
