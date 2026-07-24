"""需求质控路由

符合合并后的接口设计：
  POST /api/v1/analysis            → 提交任务，返回 task_id
  GET  /api/v1/analysis/{task_id}  → 轮询任务状态；completed 时自动从 Redis 读取 checklist 并嵌入
"""
from fastapi import APIRouter, HTTPException, Depends, BackgroundTasks
from models import AnalysisRequest, TaskSubmitResponse, TaskStatusResponse
from services.deerflow_client import DeerFlowClient
from services.task_manager import create_task_manager, TaskStatus
from services.redis_qc_client import RedisQcClient, build_qc_plan_key
from middleware.auth import verify_api_key
from config import AGENT_NAME, SKILL_NAME, get_redis_client
import json

router = APIRouter(prefix="/api/v1/analysis", tags=["需求质控"])

# singletons
task_manager = create_task_manager()
deerflow_client = DeerFlowClient()
redis_client = get_redis_client()
redis_qc_client = RedisQcClient(redis_client) if redis_client else None


# ── 提交任务 ──

@router.post("", response_model=TaskSubmitResponse)
def submit_analysis(
    req: AnalysisRequest,
    background_tasks: BackgroundTasks,
    _: None = Depends(verify_api_key),
):
    """提交需求质控任务（异步）

    触发 DeerFlow req-analysis Agent 下的 auto-req-analysis Skill，
    Skill 将质控结果写入 Redis。
    调用方通过 GET /api/v1/analysis/{task_id} 轮询结果。
    """
    task_id = task_manager.create_task("analysis", req.model_dump())
    background_tasks.add_task(_run_analysis_task, task_id, req)
    return TaskSubmitResponse(task_id=task_id, status="pending")


# ── 查询任务状态（含 checklist 嵌入） ──

@router.get("/{task_id}", response_model=TaskStatusResponse)
def get_analysis_result(task_id: str, _: None = Depends(verify_api_key)):
    """查询任务状态

    - pending/processing → 仅返回状态
    - completed → 从 Redis 读取 checklist 并嵌入响应
    - failed → 返回错误信息
    """
    task = task_manager.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail=f"任务 {task_id} 不存在")

    # 基础响应
    response = TaskStatusResponse(
        task_id=task["task_id"],
        status=task["status"],
        error=task.get("error"),
    )

    # completed 时从 Redis 读取 checklist
    if task["status"] == TaskStatus.COMPLETED:
        redis_key = task.get("result", {}).get("redis_key") if task.get("result") else None
        if redis_key:
            checklist = _read_checklist_from_redis(redis_key)
            if checklist is not None:
                response.checklist = checklist
            else:
                # Redis 数据异常：key 不存在/过期/缺少 checklist
                response.status = TaskStatus.FAILED
                response.error = f"质控结果数据异常（Redis key: {redis_key}）"
        else:
            response.status = TaskStatus.FAILED
            response.error = "任务结果中缺少 redis_key"

    return response


# ── 后台任务 ──

def _run_analysis_task(task_id: str, req: AnalysisRequest):
    """后台执行：调用 DeerFlow Agent 执行质控"""
    task_manager.update_task(task_id, TaskStatus.PROCESSING)

    try:
        redis_key = build_qc_plan_key(req.collection_name, req.work_item_id)

        message = json.dumps({
            "action": "auto_req_analysis",
            "collection_name": req.collection_name,
            "work_item_id": req.work_item_id,
            "tfs_project": req.tfs_project,
            "tfs_pat": req.tfs_pat,
            "redis_key": redis_key,
        }, ensure_ascii=False)

        response_text = deerflow_client.run_agent(
            collection_name=req.collection_name,
            work_item_id=req.work_item_id,
            message=message,
            agent_name=AGENT_NAME,
        )

        # 尝试从响应中解析 Skill 返回的 JSON 摘要
        try:
            skill_response = json.loads(response_text)
        except (json.JSONDecodeError, TypeError):
            skill_response = {}

        task_manager.update_task(task_id, TaskStatus.COMPLETED, result={
            "redis_key": redis_key,
            "skill_status": skill_response.get("status", "unknown"),
        })

    except Exception as e:
        task_manager.update_task(task_id, TaskStatus.FAILED, error=str(e))


# ── 内部工具 ──

def _read_checklist_from_redis(redis_key: str):
    """从 Redis 读取指定 key 的 checklist 节点

    优先使用 redis_qc_client（如果有 Redis 连接），
    否则返回 None（由调用方处理为 failed）。
    """
    if redis_qc_client is None:
        return None

    raw = redis_qc_client._redis.get(redis_key)
    if not raw:
        return None

    try:
        decoded = raw.decode("utf-8") if isinstance(raw, bytes) else raw
        payload = json.loads(decoded)
    except (json.JSONDecodeError, UnicodeDecodeError, TypeError):
        return None

    return payload.get("checklist")
