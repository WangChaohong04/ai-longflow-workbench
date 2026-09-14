"""恢复循环：进程重启后继续未完成工作；后台并行推进有上限。

采用的是持久化状态（waiting_approval / waiting_event + scheduled_at），
worker 只做轻量 tick，不在等待中空转调用模型——没有可推进任务时立即休眠。

并发（问题5）：
- 用有界线程池并行推进**多个根任务**（每个根用独立 engine/连接），默认并发上限 4，
  可经 limits.worker_concurrency 配置；
- 原子领取：_active 集合保证同一根不会在同一时刻被两个线程重复 tick；
- 取消保护：底层 orchestrator 用状态守卫，迟到结果不会覆盖已取消/已完成状态；
- 重启恢复：进程启动即 recover 一次，用持久化状态继续，不重复已完成动作。
"""
from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor

from . import events
from .models import (IN_PROGRESS, PENDING, READY, RETRYING, WAITING_EVENT)


class RecoveryWorker:
    def __init__(self, engine_factory, interval_seconds: float = 2.0,
                 max_parallel: int = 4):
        self._factory = engine_factory  # () -> Engine（每次新连接）
        self._interval = interval_seconds
        self._max_parallel = max(1, int(max_parallel or 4))
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._pool: ThreadPoolExecutor | None = None
        self._active: set[str] = set()      # 正在被 tick 的 root_id（原子领取）
        self._lock = threading.Lock()

    # ---------- 生命周期 ----------

    def start(self) -> None:
        self._pool = ThreadPoolExecutor(max_workers=self._max_parallel,
                                        thread_name_prefix="lf-recovery")
        self._thread = threading.Thread(target=self._run, name="longflow-recovery",
                                        daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)
        if self._pool:
            self._pool.shutdown(wait=False, cancel_futures=True)
            self._pool = None

    # ---------- 主循环 ----------

    def _run(self) -> None:
        self._recover_all()          # 启动即恢复一次（进程重启场景）
        while not self._stop.wait(self._interval):
            self._recover_all()

    def _collect_root_ids(self) -> list[str]:
        engine = self._factory()
        try:
            conn = engine.conn
            rows = conn.execute(
                """SELECT DISTINCT root_id FROM tasks
                   WHERE status IN (?, ?, ?, ?) OR root_id IN
                   (SELECT root_id FROM tasks WHERE status IN (?, ?, ?, ?))""",
                (IN_PROGRESS, PENDING, READY, WAITING_EVENT,
                 IN_PROGRESS, PENDING, READY, RETRYING),
            ).fetchall()
            return [r["root_id"] for r in rows]
        finally:
            try:
                engine.conn.close()
            except Exception:  # noqa: BLE001
                pass

    def _recover_all(self) -> None:
        try:
            roots = self._collect_root_ids()
        except Exception:  # noqa: BLE001 - 收集失败不退出 worker
            time.sleep(1)
            return
        # 原子领取：跳过已在处理的根，避免并发重复执行同一根
        with self._lock:
            pending = [r for r in roots if r not in self._active]
            for r in pending:
                self._active.add(r)
        if not pending:
            return
        if self._max_parallel <= 1 or self._pool is None:
            for r in pending:
                self._tick_root(r)
            return
        for r in pending:
            if self._stop.is_set():
                break
            self._pool.submit(self._tick_root, r)

    def _tick_root(self, root_id: str) -> None:
        engine = self._factory()
        try:
            try:
                engine.tick(root_id)
            except Exception as exc:  # noqa: BLE001 - 单根失败不拖垮其他根
                try:
                    events.emit(engine.conn, events.ERROR, task_id=root_id,
                                actor="recovery", detail={"error": str(exc)[:300]})
                except Exception:  # noqa: BLE001
                    pass
        finally:
            try:
                engine.conn.close()
            except Exception:  # noqa: BLE001
                pass
            with self._lock:
                self._active.discard(root_id)