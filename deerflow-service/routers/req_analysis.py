"""需求分析路由模块

包含评估（异步提交 + 轮询）和生成（异步提交 + 轮询，仅重试）。
正常流程：提交评估 → 轮询 → confirmed 时自动触发生成 → 轮询拿到文档。
"""
from fastapi import APIRouter, HTTPException, Depends, BackgroundTasks
from models import (
    EvaluationRequest,
    GenerationStartRequest,
    TaskSubmitResponse,
    TaskStatusResponse,
)
from services.deerflow_client import DeerFlowClient
from services.session_manager import SessionManager
from services.task_manager import create_task_manager, TaskStatus
from middleware.auth import verify_api_key
from config import AGENT_NAME
import json
import re

router = APIRouter(prefix="/api/v1/req-analysis", tags=["需求分析"])

# 单例
task_manager = create_task_manager()
deerflow_client = DeerFlowClient()
session_manager = SessionManager()


# ── 评估 ──

@router.post("/evaluation", response_model=TaskSubmitResponse)
def submit_evaluation(
    req: EvaluationRequest,
    background_tasks: BackgroundTasks,
    _: None = Depends(verify_api_key),
):
    """
    提交评估任务（异步）。

    - 无 answers → 首次评估任务
    - 有 answers → 继续评估任务
    - 返回 task_id，调用方通过 GET /req-analysis/evaluation/{task_id} 轮询结果
    """
    task_id = task_manager.create_task("evaluation", req.model_dump())
    background_tasks.add_task(_run_evaluation_task, task_id, req)
    return TaskSubmitResponse(task_id=task_id, status="pending")


@router.get("/evaluation/{task_id}", response_model=TaskStatusResponse)
def get_evaluation_result(task_id: str, _: None = Depends(verify_api_key)):
    """轮询评估任务状态和结果。"""
    task = task_manager.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail=f"任务 {task_id} 不存在")
    return TaskStatusResponse(
        task_id=task["task_id"],
        status=task["status"],
        result=task.get("result"),
        error=task.get("error"),
    )


def _run_evaluation_task(task_id: str, req: EvaluationRequest):
    """后台线程：执行评估任务"""
    task_manager.update_task(task_id, TaskStatus.PROCESSING)

    try:
        client = deerflow_client
        thread_id = client.thread_id(req.collection_name, req.work_item_id)

        # 构造 message
        if req.answers:
            previous = session_manager.get_session(thread_id)
            message = json.dumps({
                "action": "continue_evaluation",
                "previous_evaluation": previous.get("data", {}) if previous else {},
                "answers": [a.model_dump() for a in req.answers],
            }, ensure_ascii=False)
        else:
            message = f"work_item_id: {req.work_item_id}"

        # 调用 Agent
        response_text = client.run_agent(
            collection_name=req.collection_name,
            work_item_id=req.work_item_id,
            message=message,
            agent_name=AGENT_NAME,
        )

        # 解析评估结果
        result = _parse_evaluation_output(response_text)
        eva_status = result.get("evaluation_status", "unknown")
        session_manager.update_status(thread_id, status=eva_status, data=result)

        # 评估已确认 → 自动触发生成
        doc_content = ""
        gen_status = ""
        if eva_status == "confirmed":
            gen_message = json.dumps({
                "action": "generate_and_write_back",
                "work_item_id": req.work_item_id,
                "collection_name": req.collection_name,
                "tfs_pat": req.tfs_pat,
                "tfs_project": req.tfs_project,
            }, ensure_ascii=False)

            gen_response = client.run_agent(
                collection_name=req.collection_name,
                work_item_id=req.work_item_id,
                message=gen_message,
                agent_name=AGENT_NAME,
            )

            tfs_result = _parse_tfs_result(gen_response)
            doc_content = tfs_result.get("document_content", "")
            gen_status = tfs_result.get("tfs_status", "")

        # 组装最终结果
        final_result = {
            "thread_id": thread_id,
            "work_item_id": req.work_item_id,
            "evaluation_status": eva_status,
            "requirement_summary": result.get("requirement_summary", ""),
            "evaluation_report": result.get("evaluation_report", ""),
            "pending_questions": result.get("pending_questions", []),
            "confirmed_requirements": result.get("confirmed_requirements", ""),
            "statistics": result.get("statistics", {}),
            "document_content": doc_content,
            "generation_status": gen_status,
        }

        task_manager.update_task(task_id, TaskStatus.COMPLETED, result=final_result)

    except Exception as e:
        task_manager.update_task(task_id, TaskStatus.FAILED, error=str(e))


# ── 生成（仅重试） ──

@router.post("/generation", response_model=TaskSubmitResponse)
def submit_generation(
    req: GenerationStartRequest,
    background_tasks: BackgroundTasks,
    _: None = Depends(verify_api_key),
):
    """
    提交生成任务（异步，仅重试用）。
    正常流程由 evaluation 内部自动触发。
    """
    thread_id = deerflow_client.thread_id(req.collection_name, req.work_item_id)
    session = session_manager.get_session(thread_id)
    if not session:
        raise HTTPException(status_code=404, detail="评估会话不存在，请先完成需求评估")
    if session.get("status") != "confirmed":
        raise HTTPException(
            status_code=400,
            detail="评估尚未完成，请确认所有待处理问题后再触发生成"
        )

    task_id = task_manager.create_task("generation", req.model_dump())
    background_tasks.add_task(_run_generation_task, task_id, req)
    return TaskSubmitResponse(task_id=task_id, status="pending")


@router.get("/generation/{task_id}", response_model=TaskStatusResponse)
def get_generation_result(task_id: str, _: None = Depends(verify_api_key)):
    """轮询生成任务状态和结果。"""
    task = task_manager.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail=f"任务 {task_id} 不存在")
    return TaskStatusResponse(
        task_id=task["task_id"],
        status=task["status"],
        result=task.get("result"),
        error=task.get("error"),
    )


def _run_generation_task(task_id: str, req: GenerationStartRequest):
    """后台线程：执行生成任务"""
    task_manager.update_task(task_id, TaskStatus.PROCESSING)

    try:
        client = deerflow_client
        thread_id = client.thread_id(req.collection_name, req.work_item_id)

        message = json.dumps({
            "action": "generate_and_write_back",
            "work_item_id": req.work_item_id,
            "collection_name": req.collection_name,
            "tfs_pat": req.tfs_pat,
            "tfs_project": req.tfs_project,
        }, ensure_ascii=False)

        response_text = client.run_agent(
            collection_name=req.collection_name,
            work_item_id=req.work_item_id,
            message=message,
            agent_name=AGENT_NAME,
        )

        tfs_result = _parse_tfs_result(response_text)

        session_manager.update_status(thread_id, status="generated", data={
            "work_item_id": req.work_item_id,
            "tfs_status": tfs_result.get("tfs_status"),
        })

        final_result = {
            "thread_id": thread_id,
            "work_item_id": req.work_item_id,
            "status": tfs_result.get("tfs_status", "unknown"),
            "message": tfs_result.get("tfs_message", ""),
            "document_content": tfs_result.get("document_content", ""),
            "tfs_result": tfs_result,
        }

        task_manager.update_task(task_id, TaskStatus.COMPLETED, result=final_result)

    except Exception as e:
        task_manager.update_task(task_id, TaskStatus.FAILED, error=str(e))


# ── 解析工具函数 ──

def _parse_evaluation_output(text: str) -> dict:
    """从 Agent 响应中解析评估 JSON"""
    json_match = re.search(r'```json\s*([\s\S]*?)\s*```', text)
    if json_match:
        return json.loads(json_match.group(1))
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    brace_match = re.search(r'\{[\s\S]*\}', text)
    if brace_match:
        return json.loads(brace_match.group(0))
    raise ValueError("无法从输出中解析结构化 JSON")


def _parse_tfs_result(text: str) -> dict:
    """从 Agent 响应中提取 Skill 内部的 TFS 回写结果"""
    match = re.search(r'<tfs_result>\s*([\s\S]*?)\s*</tfs_result>', text)
    if match:
        try:
            return json.loads(match.group(1))
        except json.JSONDecodeError:
            return {"tfs_status": "parse_error", "document_content": match.group(1).strip()}
    # 降级：从 <doc_result> 提取（Skill 回写失败时的降级输出）
    match = re.search(r'<doc_result>\s*([\s\S]*?)\s*</doc_result>', text)
    if match:
        try:
            data = json.loads(match.group(1))
            return {
                "tfs_status": "tfs_write_failed",
                "document_content": data.get("content", ""),
            }
        except json.JSONDecodeError:
            return {
                "tfs_status": "tfs_write_failed",
                "document_content": match.group(1).strip(),
            }
    return {"tfs_status": "generation_empty", "document_content": ""}
