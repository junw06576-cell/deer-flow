"""DeerFlow LangGraph API 同步调用封装。

线程由 collection_name + work_item_id 拼接："{collection_name}-{work_item_id}"，
DeerFlow 首次收到时自动创建 Thread，后续复用。

注意（2026-08-13 故障复盘）：
- 本部署的 Gateway 版本对 `POST /threads/{tid}/runs`（非阻塞创建后台 run）返回
  501 Not Implemented，仅支持 `POST /threads/{tid}/runs/wait`（阻塞到完成）。
- 因此这里使用 wait + 大超时（1800s），配合 analysis.py 的 plan run_id 对账
  （agent 未写新结果时不得判成功），避免"瞬间完成 + 返回旧结果"的假成功。
- 待 Gateway 升级支持 POST /runs 后，可切回非阻塞 + 轮询（见 git 历史
  run_poller/run_registry）。
"""

import httpx
import secrets
from urllib.parse import quote
from config import (
    DEERFLOW_LANGGRAPH_URL,
    DEER_FLOW_INTERNAL_AUTH_TOKEN,
    DEER_FLOW_OWNER_USER_ID,
)


class DeerFlowClient:
    """DeerFlow LangGraph API 同步调用封装。"""

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

    def run_agent(
        self,
        collection_name: str,
        work_item_id: int,
        message: str,
        agent_name: str,
        timeout: float = 1800.0,
    ) -> str:
        """
        在指定 Thread 中运行 Agent，阻塞等待直到 run 完成并返回完整响应。

        使用 POST /runs/wait（本部署 Gateway 唯一支持的方式，POST /runs 返回 501）。
        超时放宽到 1800s（最长 run 约 11 分钟），避免旧的 600s 硬超时撞穿导致的假失败。

        Args:
            collection_name: TFS 集合名称（如 WN_Data_Platform），用于构造 thread_id
            work_item_id: TFS 工作项 ID
            message: 发送给 Agent 的消息
            agent_name: Agent 名称（固定为 "req-analysis"）
            timeout: 阻塞等待总超时（秒）

        Returns:
            Agent 的完整文本响应（LangGraph thread state JSON）
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
        }

        resp = self._client.post(
            f"{self.base_url}/threads/{quote(tid, safe='')}/runs/wait",
            json=payload,
            headers={"X-CSRF-Token": self._csrf_token},
            timeout=timeout,
        )
        resp.raise_for_status()
        return resp.text

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

    def close(self):
        self._client.close()
