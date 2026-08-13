import json
import time

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException

from config import AGENT_NAME, get_redis_client
from middleware.auth import verify_api_key
from models import AnalysisRequest, TaskStatusResponse, TaskSubmitResponse
from services.deerflow_client import _RUN_POLL_TIMEOUT_SECONDS, DeerFlowClient
from services.redis_qc_client import RedisQcClient, build_qc_plan_key
from services.run_registry import run_registry
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
    """Run DeerFlow agent in the background."""
    task_manager.update_task(task_id, TaskStatus.PROCESSING)
    redis_key = build_qc_plan_key(req.collection_name, req.work_item_id)

    try:
        request_payload = {
            "action": "auto_req_analysis",
            "collection_name": req.collection_name,
            "work_item_id": req.work_item_id,
            "tfs_project": req.tfs_project,
            "tfs_pat": req.tfs_pat,
            "redis_key": redis_key,
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

        run_id = deerflow_client.start_run(
            collection_name=req.collection_name,
            work_item_id=req.work_item_id,
            message=message,
            agent_name=AGENT_NAME,
        )

        # 登记到调度注册表：由 run_poller 调度线程统一轮询并收尾（对账 Redis）。
        # 本线程到此返回，不再占用后台线程等待。
        run_registry.register(
            task_id=task_id,
            run_id=run_id,
            tid=deerflow_client.thread_id(req.collection_name, req.work_item_id),
            redis_key=redis_key,
            deadline=time.time() + _RUN_POLL_TIMEOUT_SECONDS,  # epoch 秒，跨进程可比较
        )

    except Exception as exc:
        # 点火失败（如 POST /runs 异常）：对账 Redis，agent 可能已写盘，避免假失败。
        if _read_result_from_redis(redis_key) is not None:
            task_manager.update_task(
                task_id,
                TaskStatus.COMPLETED,
                result={
                    "redis_key": redis_key,
                    "skill_status": "success(reconciled)",
                },
            )
        else:
            task_manager.update_task(task_id, TaskStatus.FAILED, error=str(exc))


def _read_result_from_redis(redis_key: str):
    if redis_qc_client is None:
        return None
    return redis_qc_client.get_result_by_key(redis_key)
