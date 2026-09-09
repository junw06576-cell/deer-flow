import json
import time

import httpx
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException

from config import AGENT_NAME, get_redis_client
from middleware.auth import verify_api_key
from models import AnalysisRequest, TaskStatusResponse, TaskSubmitResponse
from services.deerflow_client import DeerFlowClient
from services.redis_qc_client import RedisQcClient, build_qc_plan_key
from services.task_manager import TaskStatus, create_task_manager

router = APIRouter(prefix="/api/v1/analysis", tags=["analysis"])

task_manager = create_task_manager()
deerflow_client = DeerFlowClient()
redis_client = get_redis_client()
redis_qc_client = RedisQcClient(redis_client) if redis_client else None


@router.post("", response_model=TaskSubmitResponse)
def submit_analysis(
    req: AnalysisRequest,
    background_tasks: BackgroundTasks,
    _: None = Depends(verify_api_key),
):
    """Submit an asynchronous requirement analysis task."""
    task_id = task_manager.create_task("analysis", req.model_dump())
    background_tasks.add_task(_run_analysis_task, task_id, req)
    return TaskSubmitResponse(task_id=task_id, status="pending")


@router.get("/{task_id}", response_model=TaskStatusResponse)
def get_analysis_result(task_id: str, _: None = Depends(verify_api_key)):
    """Poll an analysis task and include the full Redis result when available."""
    task = task_manager.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail=f"Task {task_id} not found")

    response = TaskStatusResponse(
        task_id=task["task_id"],
        status=task["status"],
        error=task.get("error"),
    )

    if task["status"] == TaskStatus.COMPLETED:
        redis_key = task.get("result", {}).get("redis_key") if task.get("result") else None
        if not redis_key:
            response.status = TaskStatus.FAILED
            response.error = "Task result is missing redis_key"
            return response

        response.redis_key = redis_key
        redis_result = _read_result_from_redis(redis_key)
        if redis_result is None:
            response.status = TaskStatus.FAILED
            response.error = f"Analysis result data is unavailable (Redis key: {redis_key})"
            return response

        response.redis_result = redis_result

    return response


def _run_analysis_task(task_id: str, req: AnalysisRequest):
    """Run DeerFlow agent in the background（阻塞 wait + 超时兜底 + plan run_id 对账）。"""
    task_manager.update_task(task_id, TaskStatus.PROCESSING)
    redis_key = build_qc_plan_key(req.collection_name, req.work_item_id)
    baseline = _plan_probe(redis_key)  # (run_id, generated_at_utc) 本次提交前的落盘基线

    timeout_happened = False

    try:
        request_payload = {
            "action": "auto_req_analysis",
            "collection_name": req.collection_name,
            "work_item_id": req.work_item_id,
            "tfs_project": req.tfs_project,
            "redis_key": redis_key,
        }

        # request-scoped secrets：键名对齐 tfs_client.py 的优先级（--pat > TFS_PAT > tfs.pat）
        tfs_secrets = {
            "TFS_PAT": req.tfs_pat,
            "TFS_COLLECTION": req.collection_name,
            "TFS_PROJECT": req.tfs_project,
        }

        if req.human_feedback:
            request_payload["human_feedback"] = req.human_feedback
            parts = [
                "This is a re-submission after human review. "
                "The previous analysis and questions are in the conversation history above. "
                "Human feedback on those questions is provided in the 'human_feedback' field below. "
                "Based on the feedback, update the QC result, revise the Redis plan if needed, "
                "and proceed to the next step. Do NOT re-run the full analysis from scratch. "
                "CRITICAL: When running pipeline.py apply, you MUST include --execute to write "
                "results (labels/state/description) to TFS. Never omit --execute.",
            ]
            if req.additional_info:
                request_payload["additional_info"] = req.additional_info
                parts.append(
                    "Additional context is provided in the 'additional_info' field — "
                    "factor this into your revision."
                )
            parts.append(f"\n\nTask input JSON:\n{json.dumps(request_payload, ensure_ascii=False)}")
            message = "\n".join(parts)
        elif req.additional_info:
            request_payload["additional_info"] = req.additional_info
            message = (
                "This is a re-analysis request with additional context. "
                "The previous analysis is in the conversation history above. "
                "Review the previous result, incorporate the additional context provided "
                "in the 'additional_info' field below, and revise the analysis accordingly. "
                "Do NOT start from scratch — build on the previous work, correct any issues "
                "identified by the additional context, and update the QC result and Redis plan. "
                "Do not ask for clarification and do not stop after summarizing this input. "
                "Write the authoritative result to Redis using the provided redis_key. "
                "CRITICAL: When running pipeline.py apply, you MUST include --execute to write "
                "results (labels/state/description) to TFS. Never omit --execute. "
                "Return only a short JSON status summary when the workflow is finished.\n\n"
                "Task input JSON:\n"
                f"{json.dumps(request_payload, ensure_ascii=False)}"
            )
        else:
            message = (
                "System automation task. Execute the requirement analysis/QC workflow "
                "using the agent's configured requirement-analysis skill. Do not ask "
                "for clarification and do not stop after summarizing this input. "
                "Read the configured skill instructions, run the required workflow, "
                "and write the authoritative result to Redis using the provided "
                "redis_key. CRITICAL: When running pipeline.py apply, you MUST include "
                "--execute to write results (labels/state/description) to TFS. Never omit "
                "--execute. Return only a short JSON status summary when the workflow "
                "is finished.\n\n"
                "Task input JSON:\n"
                f"{json.dumps(request_payload, ensure_ascii=False)}"
            )

        deerflow_client.run_agent(
            collection_name=req.collection_name,
            work_item_id=req.work_item_id,
            message=message,
            agent_name=AGENT_NAME,
            tfs_secrets=tfs_secrets,
        )

    except httpx.ReadTimeout:
        # 超时：agent 可能还在跑，给兜底窗口等 Redis 落盘
        timeout_happened = True
    except Exception as exc:
        # wait 其它异常：若 agent 实际已写盘，按成功处理
        if _plan_probe(redis_key) != baseline:
            task_manager.update_task(
                task_id,
                TaskStatus.COMPLETED,
                result={"redis_key": redis_key, "skill_status": "success(reconciled)"},
            )
        else:
            task_manager.update_task(
                task_id,
                TaskStatus.FAILED,
                error=f"AGENT_TASK_EXCEPTION: Agent 运行异常——{exc}",
            )
        return

    # ── 对账阶段 ──
    # 超时后兜底轮询：agent 可能还在跑，给一个收尾窗口（最多等 60s）让 Redis 落盘
    if timeout_happened:
        _grace_probe(redis_key, baseline, timeout=60.0)

    if _plan_probe(redis_key) != baseline:
        task_manager.update_task(
            task_id,
            TaskStatus.COMPLETED,
            result={
                "redis_key": redis_key,
                "skill_status": "success(reconciled)" if timeout_happened else "success",
            },
        )
    else:
        error_msg = (
            "AGENT_RESULT_NOT_FRESH: Agent 已结束但未写入新结果。"
            "可能原因：Agent 未执行到写入步骤、publish_plan 失败降级、"
            "或结果与上一轮完全相同。建议重新提交任务，"
            "或在工作项中补充信息后重试。"
        )
        task_manager.update_task(
            task_id,
            TaskStatus.FAILED,
            error=error_msg,
        )


def _grace_probe(redis_key: str, baseline, timeout: float = 60.0):
    """超时后兜底轮询：等 Redis 落盘，最大等 timeout 秒。

    每次间隔 5s 查一次，若 run_id/generated_at_utc 任一变化立即返回。
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        if _plan_probe(redis_key) != baseline:
            return
        time.sleep(5)


def _plan_probe(redis_key: str):
    """读取 plan key 的落盘信号，用于对账判断本次是否真的写入新结果。

    返回 (run_id, generated_at_utc) 二元组。agent 每次 publish_plan 落盘时，
    `run_id` 与 `generated_at_utc`（UTC 时间戳）都会同时刷新；两个信号任一变
    化即可认定"本 run 写入了新结果"，避免 agent 复用历史 run_id 时被误判失败
    （2026-08-13 假成功 / 2026-08-25 假失败复盘）。
    """
    if redis_client is None:
        return (None, None)

    def _get(field):
        val = redis_client.hget(redis_key, field)
        if val is None:
            return None
        return val.decode() if isinstance(val, bytes) else val

    return (_get("run_id"), _get("generated_at_utc"))


def _plan_run_id(redis_key: str):
    """兼容占位：仅读 run_id。新代码应优先使用 _plan_probe 的双信号。"""
    return _plan_probe(redis_key)[0]


def _read_result_from_redis(redis_key: str):
    if redis_qc_client is None:
        return None
    return redis_qc_client.get_result_by_key(redis_key)