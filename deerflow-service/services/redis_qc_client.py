"""Redis QC result reader.

Reads checklist data written by requirement-analysis skills.
"""

import json
from typing import Any, Optional, Dict

from config import REDIS_QC_KEY_PREFIX


def build_qc_plan_key(collection_name: str, work_item_id: int) -> str:
    """Build Redis key: auto-req:qc:plan:{collection}:{work_item_id}."""
    return f"{REDIS_QC_KEY_PREFIX}:{collection_name}:{work_item_id}"


class RedisQcClient:
    """Read QC checklist results from Redis.

    Supported storage contracts:
      - string value: JSON object with top-level ``checklist``
      - hash value: field ``checklist`` containing checklist JSON
    """

    def __init__(self, redis_client):
        self._redis = redis_client

    def get_checklist(self, collection_name: str, work_item_id: int) -> Optional[Dict[str, Any]]:
        key = build_qc_plan_key(collection_name, work_item_id)
        return self.get_checklist_by_key(key)

    def get_checklist_by_key(self, key: str) -> Optional[Dict[str, Any]]:
        result = self.get_result_by_key(key)
        if not result:
            return None
        checklist = result.get("checklist")
        return checklist if isinstance(checklist, dict) else None

    def get_result_by_key(self, key: str) -> Optional[Dict[str, Any]]:
        try:
            key_type = self._redis.type(key)
        except Exception:
            return None

        if isinstance(key_type, bytes):
            key_type = key_type.decode("utf-8", errors="replace")

        if key_type == "string":
            value = self._redis.get(key)
            decoded = _decode_json(value)
            if isinstance(decoded, dict):
                return decoded
            text = _decode_text(value)
            return {"value": text} if text is not None else None

        if key_type == "hash":
            raw_hash = self._redis.hgetall(key)
            result: Dict[str, Any] = {}
            for raw_key, raw_value in raw_hash.items():
                field = _decode_text(raw_key)
                if field is None:
                    continue
                value = _decode_text(raw_value)
                if field == "checklist":
                    parsed = _decode_json(raw_value)
                    result[field] = parsed if parsed is not None else value
                else:
                    result[field] = value
            return result

        return None

    def exists(self, collection_name: str, work_item_id: int) -> bool:
        key = build_qc_plan_key(collection_name, work_item_id)
        return bool(self._redis.exists(key))


def _decode_text(raw) -> Optional[str]:
    if raw is None:
        return None
    if isinstance(raw, bytes):
        try:
            return raw.decode("utf-8")
        except UnicodeDecodeError:
            return None
    return raw if isinstance(raw, str) else None


def _decode_json(raw):
    decoded = _decode_text(raw)
    if decoded is None:
        return None
    try:
        return json.loads(decoded)
    except (json.JSONDecodeError, TypeError):
        return None


def _decode_json_checklist(raw) -> Optional[Dict[str, Any]]:
    payload = _decode_json(raw)
    if not isinstance(payload, dict):
        return None
    checklist = payload.get("checklist")
    return checklist if isinstance(checklist, dict) else None
