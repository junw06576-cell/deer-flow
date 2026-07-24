import uuid
import time
import json
import threading
from typing import Optional, Dict
from enum import Enum

from config import REDIS_URL

class TaskStatus(str, Enum):
    PENDING = "pending"
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"


# ── Redis 版（生产环境） ──

class RedisTaskManager:
    """异步任务管理器（Redis 实现）

    支持多 worker 共享任务状态，任务自动过期（24h）。

    Redis Key 格式：task:{task_id}
    存储结构：Hash，字段包括 status/result/error/params/task_type/created_at/updated_at
    """

    TASK_PREFIX = "task:"
    TASK_EXPIRE_SECONDS = 86400  # 24 小时

    def __init__(self, redis_client):
        self._redis = redis_client

    def create_task(self, task_type: str, params: dict) -> str:
        task_id = uuid.uuid4().hex
        now = time.time()
        key = f"{self.TASK_PREFIX}{task_id}"
        task_data = {
            "task_id": task_id,
            "task_type": task_type,
            "status": TaskStatus.PENDING,
            "params": json.dumps(params, ensure_ascii=False),
            "result": "",
            "error": "",
            "created_at": str(now),
            "updated_at": str(now),
        }
        self._redis.hset(key, mapping=task_data)
        self._redis.expire(key, self.TASK_EXPIRE_SECONDS)
        return task_id

    def get_task(self, task_id: str) -> Optional[dict]:
        key = f"{self.TASK_PREFIX}{task_id}"
        data = self._redis.hgetall(key)
        if not data:
            return None
        # 反序列化
        result = {}
        for k, v in data.items():
            field = k.decode() if isinstance(k, bytes) else k
            value = v.decode() if isinstance(v, bytes) else v
            result[field] = value
        # 还原类型
        if result.get("result"):
            try:
                result["result"] = json.loads(result["result"])
            except json.JSONDecodeError:
                pass
        else:
            result["result"] = None
        if not result.get("error"):
            result["error"] = None
        return result

    def update_task(self, task_id: str, status: TaskStatus, result: dict = None, error: str = None):
        key = f"{self.TASK_PREFIX}{task_id}"
        updates = {"status": status, "updated_at": str(time.time())}
        if result is not None:
            updates["result"] = json.dumps(result, ensure_ascii=False)
        if error is not None:
            updates["error"] = error
        self._redis.hset(key, mapping=updates)


# ── 内存版（开发环境 fallback） ──

class InMemoryTaskManager:
    """异步任务管理器（内存实现，单进程有效）"""

    def __init__(self):
        self._tasks: Dict[str, dict] = {}
        self._lock = threading.Lock()

    def create_task(self, task_type: str, params: dict) -> str:
        task_id = uuid.uuid4().hex
        now = time.time()
        with self._lock:
            self._tasks[task_id] = {
                "task_id": task_id,
                "task_type": task_type,
                "status": TaskStatus.PENDING,
                "params": params,
                "result": None,
                "error": None,
                "created_at": now,
                "updated_at": now,
            }
        return task_id

    def get_task(self, task_id: str) -> Optional[dict]:
        return self._tasks.get(task_id)

    def update_task(self, task_id: str, status: TaskStatus, result: dict = None, error: str = None):
        with self._lock:
            if task_id in self._tasks:
                self._tasks[task_id]["status"] = status
                self._tasks[task_id]["updated_at"] = time.time()
                if result is not None:
                    self._tasks[task_id]["result"] = result
                if error is not None:
                    self._tasks[task_id]["error"] = error


# ── 工厂方法 ──

def create_task_manager():
    """根据环境变量自动选择实现

    配置了 REDIS_URL → Redis 版
    未配置 → 内存版（开发用）
    """
    if REDIS_URL:
        import redis
        client = redis.from_url(REDIS_URL, decode_responses=False)
        return RedisTaskManager(client)
    else:
        return InMemoryTaskManager()
