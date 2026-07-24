import httpx
import secrets
from config import DEERFLOW_LANGGRAPH_URL, DEER_FLOW_INTERNAL_AUTH_TOKEN


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
    ) -> str:
        """
        在指定 Thread 中运行 Agent，阻塞等待并返回完整响应。

        Thread 由 collection_name + work_item_id 构造，首次调用时 DeerFlow 自动创建。

        Args:
            collection_name: TFS 集合名称（如 WN_Data_Platform），用于构造 thread_id
            work_item_id: TFS 工作项 ID
            message: 发送给 Agent 的消息
            agent_name: Agent 名称（固定为 "req-analysis"）

        Returns:
            Agent 的完整文本响应
        """
        tid = self.thread_id(collection_name, work_item_id)

        payload = {
            "message": message,
            "config": {
                "configurable": {
                    "agent_name": agent_name,
                    "thread_id": tid,
                }
            }
        }

        resp = self._client.post(
            f"{self.base_url}/runs/wait",
            json=payload,
            headers={"X-CSRF-Token": self._csrf_token},
            timeout=600.0,
        )
        resp.raise_for_status()
        return resp.text

    def close(self):
        self._client.close()
