"""SSE streaming proxy — 让浏览器通过 deerflow-service 消费 Gateway SSE 流。

deerflow-service 持有 X-DeerFlow-Internal-Token，浏览器没有。
这个 endpoint 充当代理：浏览器 → deerflow-service → Gateway SSE → 浏览器。
"""

import json
import logging
import httpx
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse
from urllib.parse import quote

from config import (
    DEERFLOW_LANGGRAPH_URL,
    API_KEY,
    DEER_FLOW_INTERNAL_AUTH_TOKEN,
    DEER_FLOW_OWNER_USER_ID,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1/analysis", tags=["streaming"])


@router.post("/stream")
async def stream_analysis(request: Request):
    """提交分析任务并返回 SSE 流。

    和 POST /api/v1/analysis 一样接收参数，但返回 SSE 流而非 task_id。
    浏览器直接消费这个 SSE 流，实时看到 agent 的每个阶段事件。
    """
    body = await request.json()
    collection_name = body.get("collection_name", "WN_Data_Platform")
    work_item_id = body.get("work_item_id", 0)
    tfs_project = body.get("tfs_project", "")
    tfs_pat = body.get("tfs_pat", "")
    agent_name = body.get("agent_name", "auto-analysis-agent")
    stream_mode = body.get("stream_mode", None)
    prompt_text = body.get("prompt", None)

    if not work_item_id:
        raise HTTPException(status_code=400, detail="work_item_id is required")
    if not tfs_pat:
        raise HTTPException(status_code=400, detail="tfs_pat is required")

    tid = f"{collection_name}-{work_item_id}"

    # 构建默认 prompt
    if not prompt_text:
        redis_key = f"auto-req:qc:plan:{collection_name}:{work_item_id}"
        prompt_text = (
            "System automation task. Execute the requirement analysis/QC workflow "
            "using the agent's configured requirement-analysis skill. "
            "Do not ask for clarification and do not stop after summarizing this input. "
            "Read the configured skill instructions, run the required workflow, "
            "and write the authoritative result to Redis using the provided redis_key. "
            "Return only a short JSON status summary when the workflow is finished.\n\n"
            f"Task input JSON:\n"
            f'{{"action": "auto_req_analysis", "collection_name": "{collection_name}", '
            f'"work_item_id": {work_item_id}, "tfs_project": "{tfs_project}", '
            f'"tfs_pat": "{tfs_pat}", "redis_key": "{redis_key}"}}'
        )

    # 默认 stream mode
    stream_modes = ["values", "messages-tuple"]
    if stream_mode:
        stream_modes = [s.strip() for s in stream_mode.split(",") if s.strip()]

    # SSE 流生成器 — 从 Gateway 读 SSE，逐事件转发给浏览器
    async def sse_proxy():
        async with httpx.AsyncClient(timeout=600.0) as client:
            # 1. 确保 thread 存在
            headers = {"Content-Type": "application/json", "X-CSRF-Token": "proxy-csrf"}
            if DEER_FLOW_INTERNAL_AUTH_TOKEN:
                headers["X-DeerFlow-Internal-Token"] = DEER_FLOW_INTERNAL_AUTH_TOKEN
            if DEER_FLOW_OWNER_USER_ID:
                headers["X-DeerFlow-Owner-User-Id"] = DEER_FLOW_OWNER_USER_ID

            thread_payload = {
                "thread_id": tid,
                "assistant_id": "lead_agent",
                "metadata": {
                    "source": "deerflow-service-stream",
                    "collection_name": collection_name,
                    "work_item_id": work_item_id,
                },
            }
            try:
                tr = await client.post(
                    f"{DEERFLOW_LANGGRAPH_URL}/threads",
                    json=thread_payload,
                    headers=headers,
                    cookies={"csrf_token": "proxy-csrf"},
                    timeout=30.0,
                )
                if tr.status_code == 409:
                    # thread 已存在，可以继续
                    pass
                elif tr.status_code >= 400:
                    yield f"event: error\ndata: {json.dumps({'detail': f'Thread creation failed: {tr.status_code} {tr.text[:200]}'})}\n\n"
                    return
            except Exception as e:
                yield f"event: error\ndata: {json.dumps({'detail': f'Thread creation error: {str(e)}'})}\n\n"
                return

            # 2. 启动 stream run
            context = {
                "agent_name": agent_name,
                "thread_id": tid,
                "non_interactive": True,
                "secrets": {
                    "TFS_PAT": tfs_pat,
                    "TFS_COLLECTION": collection_name,
                    "TFS_PROJECT": tfs_project,
                },
            }
            run_payload = {
                "input": {
                    "messages": [{"role": "user", "content": prompt_text}],
                },
                "context": context,
                "config": {
                    "context": context,
                    "recursion_limit": 500,
                },
                "stream_mode": stream_modes,
                "stream_subgraphs": True,
                "on_disconnect": "cancel",
            }

            try:
                async with client.stream(
                    "POST",
                    f"{DEERFLOW_LANGGRAPH_URL}/threads/{quote(tid, safe='')}/runs/stream",
                    json=run_payload,
                    headers=headers,
                    cookies={"csrf_token": "proxy-csrf"},
                    timeout=600.0,
                ) as resp:
                    if resp.status_code >= 400:
                        err_body = await resp.aread()
                        yield f"event: error\ndata: {json.dumps({'detail': f'Gateway error: {resp.status_code} {err_body[:300].decode()}'})}\n\n"
                        return

                    # 逐行转发 SSE
                    buffer = b""
                    event_buf = b""
                    async for chunk in resp.aiter_bytes():
                        buffer += chunk
                        while b"\n\n" in buffer:
                            raw_event, buffer = buffer.split(b"\n\n", 1)
                            event_buf += raw_event + b"\n\n"
                            # 转发给浏览器
                            yield raw_event.decode("utf-8", errors="replace") + "\n\n"
                    # 剩余数据
                    if buffer.strip():
                        yield buffer.decode("utf-8", errors="replace") + "\n\n"

            except httpx.TimeoutException:
                yield f"event: error\ndata: {json.dumps({'detail': 'Gateway timeout'})}\n\n"
            except Exception as e:
                yield f"event: error\ndata: {json.dumps({'detail': f'Stream error: {str(e)}'})}\n\n"

    return StreamingResponse(
        sse_proxy(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )