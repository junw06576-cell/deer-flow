"""单调度线程：统一轮询所有活跃 Run 并收尾。

替代"每任务一个后台线程自行轮询"：
- 提交线程只做"启动 run + 登记到 run_registry"，几十秒内释放；
- 本模块的常驻调度线程每 10s 扫一遍注册表，查询终态、超时 cancel、
  对账 Redis（有落盘=成功）后更新 task 状态并移出注册表。

串行查询（单线程），N 个活跃任务 = 每轮 N 次轻量 GET /runs/{rid}；
网络抖动单次失败仅跳过，由 deadline 兜底，不误杀。
"""

import logging
import threading
import time

import httpx

from config import get_redis_client
from services.deerflow_client import (
    _TERMINAL_RUN_STATUSES,
    _RUN_POLL_TIMEOUT_SECONDS,
    DeerFlowClient,
)
from services.redis_qc_client import RedisQcClient
from services.run_registry import run_registry
from services.task_manager import TaskStatus, create_task_manager

logger = logging.getLogger(__name__)

_POLL_INTERVAL_SECONDS = 10.0

task_manager = create_task_manager()
deerflow_client = DeerFlowClient()

_redis_client = get_redis_client()
_redis_qc_client = RedisQcClient(_redis_client) if _redis_client else None


def _read_result_from_redis(redis_key: str):
    """与 analysis.py 相同的对账读法（poller 独立初始化，避免循环依赖）。"""
    if _redis_qc_client is None:
        return None
    return _redis_qc_client.get_result_by_key(redis_key)


def _finalize(item: dict, run_status: str) -> None:
    """run 到达终态/超时/丢失：对账 Redis 决定 task 终态，然后移出注册表。"""
    task_id = item["task_id"]
    redis_key = item["redis_key"]
    try:
        if _read_result_from_redis(redis_key) is not None:
            skill_status = "success" if run_status == "success" else "success(reconciled)"
            task_manager.update_task(
                task_id,
                TaskStatus.COMPLETED,
                result={"redis_key": redis_key, "skill_status": skill_status},
            )
            logger.info("task %s COMPLETED (run=%s)", task_id, run_status)
        else:
            task_manager.update_task(
                task_id,
                TaskStatus.FAILED,
                error=f"run ended with status: {run_status}",
            )
            logger.warning("task %s FAILED (run=%s, redis empty)", task_id, run_status)
    finally:
        run_registry.remove(task_id)


def _poll_once() -> None:
    for item in run_registry.snapshot():
        if time.time() >= item["deadline"]:
            deerflow_client.try_cancel_run(item["tid"], item["run_id"])
            _finalize(item, "timeout")
            continue
        try:
            status = deerflow_client.get_run_status(item["tid"], item["run_id"])
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 404:
                # run 已不存在（如 Gateway 重启丢内存记录）：以 Redis 落盘为准收尾
                _finalize(item, "lost")
            # 其它 4xx/5xx：跳过，下轮再试（deadline 兜底）
            continue
        except Exception:
            # 网络抖动等：跳过，下轮再查
            continue
        if status in _TERMINAL_RUN_STATUSES:
            _finalize(item, status)


def _poll_loop(interval: float) -> None:
    logger.info("run poller started (interval=%ss)", interval)
    while True:
        try:
            _poll_once()
        except Exception:
            logger.exception("run poller iteration failed")
        time.sleep(interval)


def start_poller(interval: float = _POLL_INTERVAL_SECONDS) -> threading.Thread:
    """启动常驻调度线程（daemon，随进程退出）。"""
    thread = threading.Thread(
        target=_poll_loop,
        args=(interval,),
        daemon=True,
        name="run-poller",
    )
    thread.start()
    return thread
