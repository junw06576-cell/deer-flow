import time
from typing import Optional, Dict


class SessionManager:
    """内存会话管理（生产环境替换为 Redis）

    只缓存评估状态元数据（pending/confirmed/generated），不创建 Thread。
    Thread 由 DeerFlow 在首次收到请求时自动创建。
    key = thread_id = "{collection_name}-{work_item_id}"
    """

    def __init__(self):
        self._sessions: Dict[str, dict] = {}

    def update_status(self, thread_id: str, status: str, data: dict = None):
        """更新会话状态。首次调用时自动创建记录，后续更新。"""
        now = time.strftime("%Y-%m-%dT%H:%M:%S")
        if thread_id not in self._sessions:
            self._sessions[thread_id] = {
                "status": status,
                "created_at": now,
                "updated_at": now,
                "data": data or {},
            }
        else:
            self._sessions[thread_id]["status"] = status
            self._sessions[thread_id]["updated_at"] = now
            if data:
                self._sessions[thread_id]["data"] = data

    def get_session(self, thread_id: str) -> Optional[dict]:
        """获取会话状态"""
        return self._sessions.get(thread_id)
