"""Redis 质控结果读取客户端

从 Redis 读取 auto-req-analysis Skill 写入的质控结果（checklist）。
"""
import json
from typing import Optional, Dict, Any
from config import REDIS_QC_KEY_PREFIX


def build_qc_plan_key(collection_name: str, work_item_id: int) -> str:
    """构造 Redis key：auto-req:qc:plan:{collection}:{work_item_id}"""
    return f"{REDIS_QC_KEY_PREFIX}:{collection_name}:{work_item_id}"


class RedisQcClient:
    """从 Redis 读取质控结果"""

    def __init__(self, redis_client):
        self._redis = redis_client

    def get_checklist(self, collection_name: str, work_item_id: int) -> Optional[Dict[str, Any]]:
        """从 Redis 读取指定工作项的 checklist

        返回：
          - checklist dict（存在且包含 checklist 节点时）
          - None（key 不存在或数据不完整时）

        调用方需根据 None 判断是 not_found 还是 invalid_data。
        """
        key = build_qc_plan_key(collection_name, work_item_id)
        raw = self._redis.get(key)
        if not raw:
            return None

        try:
            decoded = raw.decode("utf-8") if isinstance(raw, bytes) else raw
            payload = json.loads(decoded)
        except (json.JSONDecodeError, UnicodeDecodeError, TypeError):
            return None

        checklist = payload.get("checklist")
        if not checklist:
            return None

        return checklist

    def exists(self, collection_name: str, work_item_id: int) -> bool:
        """检查 Redis key 是否存在"""
        key = build_qc_plan_key(collection_name, work_item_id)
        return bool(self._redis.exists(key))
