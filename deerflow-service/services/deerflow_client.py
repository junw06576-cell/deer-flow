import httpx
import secrets
from urllib.parse import quote
from config import (
    DEERFLOW_LANGGRAPH_URL,
    DEER_FLOW_INTERNAL_AUTH_TOKEN,
    DEER_FLOW_OWNER_USER_ID,
)


# Run 终态集合 + 总超时（30 分钟；当前最长 run 约 11 分钟）。
# 轮询由 services/run_poller.py 的调度线程统一执行，不再依赖本客户端。
_TERMINAL_RUN_STATUSES = {"success", "error", "timeout", "interrupted"}
_RUN_POLL_TIMEOUT_SECONDS = 1800.0


class DeerFlowClient:
    """DeerFlow LangGraph API 同步调用封装

    thread_id 由 TFS-BUDDY 传入的 collection_name 和 work_item_id 拼接：
    "{collection_name}-{work_item_id}"（如 WN_Data_Platform-233564）
    DeerFlow 首次收到时自动创建 Thread，后续复用。
    """

    def __init__(self, base_url: str = DEERFLOW_LANGGRAPH_URL):
        self.base_url = base_url
        headers = {"Content-Type": "application/json"}
        if DEER_FLOW_INTERNAL_AUTH_TOKEN:
            headers["X-DeerFlow-Internal-Token"] = DEER_FLOW_INTERNAL_AUTH_TOKEN
        if DEER_FLOW_OWNER_USER_ID:
            headers["X-DeerFlow-Owner-User-Id"] = DEER_FLOW_OWNER_USER_ID
        # 生成 CSRF double-submit cookie 对（server-to-server 调用绕过 CSRF 校验）
        self._csrf_token = secrets.token_urlsafe(64)
        self._client = httpx.Client(
            timeout=600.0,
            headers=headers,
            cookies={"csrf_token": self._csrf_token},
        )

    @staticmethod
    def thread_id(collection_name: str, work_item_id: int) -> str:
        """构造 Thread ID。collection_name 由 TFS-BUDDY 传入（集合名称）。"""
        return f"{collection_name}-{work_item_id}"

    def start_run(
        self,
        collection_name: str,
        work_item_id: int,
        message: str,
        agent_name: str,
    ) -> str:
        """
        在指定 Thread 中启动一个后台 Run，立即返回 run_id（非阻塞）。

        不再持有长连接：run 在 DeerFlow Gateway 进程中独立执行，状态查询
        由 run_poller 调度线程统一轮询（见 services/run_poller.py）。

        Args:
            collection_name: TFS 集合名称（如 WN_Data_Platform），用于构造 thread_id
            work_item_id: TFS 工作项 ID
            message: 发送给 Agent 的消息
            agent_name: Agent 名称（固定为 "req-analysis"）

        Returns:
            Gateway 返回的 run_id
        """
        tid = self.thread_id(collection_name, work_item_id)
        self._ensure_thread(tid, collection_name, work_item_id)

        context = {
            "agent_name": agent_name,
            "thread_id": tid,
            "non_interactive": True,
        }
        payload = {
            "input": {
                "messages": [
                    {
                        "role": "user",
                        "content": message,
                    }
                ]
            },
            "context": context,
            "config": {
                "context": context,
                "recursion_limit": 500,
            },
            # 同一 thread 已有 run 在跑时排队执行，避免 409 拒绝
            "multitask_strategy": "enqueue",
        }

        resp = self._client.post(
            f"{self.base_url}/threads/{quote(tid, safe='')}/runs",
            json=payload,
            headers={"X-CSRF-Token": self._csrf_token},
            timeout=60.0,
        )
        resp.raise_for_status()
        run_id = resp.json().get("run_id")
        if not run_id:
            raise RuntimeError(f"POST /runs 未返回 run_id: {resp.text[:300]}")
        return run_id

    def _ensure_thread(self, tid: str, collection_name: str, work_item_id: int) -> None:
        """幂等创建/复用 Thread（DeerFlow 首次收到时自动创建）。"""
        thread_payload = {
            "thread_id": tid,
            "assistant_id": "lead_agent",
            "metadata": {
                "source": "deerflow-service",
                "collection_name": collection_name,
                "work_item_id": work_item_id,
            },
        }
        thread_resp = self._client.post(
            f"{self.base_url}/threads",
            json=thread_payload,
            headers={"X-CSRF-Token": self._csrf_token},
            timeout=60.0,
        )
        thread_resp.raise_for_status()

    def get_run_status(self, tid: str, run_id: str) -> str:
        """查询 Run 状态；每次短超时（30s），供调度线程轮询。"""
        resp = self._client.get(
            f"{self.base_url}/threads/{quote(tid, safe='')}/runs/{quote(run_id, safe='')}",
            headers={"X-CSRF-Token": self._csrf_token},
            timeout=30.0,
        )
        resp.raise_for_status()
        return resp.json().get("status", "unknown")

    def try_cancel_run(self, tid: str, run_id: str) -> None:
        """超时后尽力取消后台 run，避免空转（失败不抛异常）。"""
        try:
            self._client.post(
                f"{self.base_url}/threads/{quote(tid, safe='')}/runs/{quote(run_id, safe='')}/cancel",
                headers={"X-CSRF-Token": self._csrf_token},
                timeout=30.0,
            )
        except Exception:
            pass

    def close(self):
        self._client.close()
