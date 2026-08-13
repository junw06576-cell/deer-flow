"""活跃 Run 注册表：单调度线程统一轮询的数据源。

- Redis 版（生产）：跨进程/重启安全。调度线程或服务重启后，遗留的活跃
  run 仍在 Redis 中，调度线程第一轮即可恢复轮询；Gateway 若已丢 run 内存
  记录则 GET 404 → 对账收尾，闭环。
- 内存版（开发 fallback）：REDIS_URL 未配置时使用，仅单进程有效。

存储结构（Redis 版）：
    Key: run:active  (Hash)
    field = task_id，value = 条目 JSON
    deadline 存 epoch 秒（wall clock），因需跨进程比较。
    TTL = 3600s 泄漏兜底（register 时设置，远大于总超时 1800s）；
    调度线程挂掉后 key 自动过期，不泄漏。
"""

import json
import threading
import time
from typing import Dict, List, Optional

from config import REDIS_URL, get_redis_client

_REDIS_KEY = "run:active"
_TTL_SECONDS = 3600  # 泄漏兜底，远大于 _RUN_POLL_TIMEOUT_SECONDS (1800s)


class MemoryRunRegistry:
    """内存版（开发环境/单进程）。"""

    def __init__(self):
        self._runs: Dict[str, dict] = {}
        self._lock = threading.Lock()

    def register(self, task_id: str, run_id: str, tid: str, redis_key: str, deadline: float) -> None:
        with self._lock:
            self._runs[task_id] = {
                "task_id": task_id,
                "run_id": run_id,
                "tid": tid,
                "redis_key": redis_key,
                "deadline": deadline,
            }

    def snapshot(self) -> List[dict]:
        with self._lock:
            return list(self._runs.values())

    def get(self, task_id: str) -> Optional[dict]:
        with self._lock:
            return self._runs.get(task_id)

    def remove(self, task_id: str) -> None:
        with self._lock:
            self._runs.pop(task_id, None)

    def size(self) -> int:
        with self._lock:
            return len(self._runs)


class RedisRunRegistry:
    """Redis 版（生产）：单 Hash key，field=task_id，value=条目 JSON。"""

    def __init__(self, redis_client):
        if redis_client is None:
            raise RuntimeError("RedisRunRegistry 需要 Redis 客户端（检查 REDIS_URL）")
        self._redis = redis_client

    def register(self, task_id: str, run_id: str, tid: str, redis_key: str, deadline: float) -> None:
        item = {
            "task_id": task_id,
            "run_id": run_id,
            "tid": tid,
            "redis_key": redis_key,
            "deadline": deadline,  # epoch 秒
        }
        self._redis.hset(_REDIS_KEY, task_id, json.dumps(item, ensure_ascii=False))
        self._redis.expire(_REDIS_KEY, _TTL_SECONDS)

    def snapshot(self) -> List[dict]:
        raw = self._redis.hgetall(_REDIS_KEY)
        items: List[dict] = []
        for field, value in (raw or {}).items():
            text = value.decode() if isinstance(value, bytes) else value
            try:
                item = json.loads(text)
            except (json.JSONDecodeError, TypeError):
                continue
            item["task_id"] = field.decode() if isinstance(field, bytes) else field
            items.append(item)
        return items

    def get(self, task_id: str) -> Optional[dict]:
        value = self._redis.hget(_REDIS_KEY, task_id)
        if not value:
            return None
        text = value.decode() if isinstance(value, bytes) else value
        try:
            return json.loads(text)
        except (json.JSONDecodeError, TypeError):
            return None

    def remove(self, task_id: str) -> None:
        self._redis.hdel(_REDIS_KEY, task_id)

    def size(self) -> int:
        return self._redis.hlen(_REDIS_KEY)


def create_run_registry():
    """根据 REDIS_URL 选择实现：生产 Redis 版，开发内存版。"""
    if REDIS_URL:
        return RedisRunRegistry(get_redis_client())
    return MemoryRunRegistry()


# 模块级单例：服务进程内共享（提交线程与调度线程共用）
run_registry = create_run_registry()
