from typing import Any, Dict, Optional

from pydantic import BaseModel, Field


class AnalysisRequest(BaseModel):
    """Request body for submitting requirement analysis."""

    collection_name: str = Field(..., description="TFS collection name, e.g. WN_Data_Platform")
    work_item_id: int = Field(..., description="TFS work item ID")
    tfs_pat: str = Field(..., description="TFS Personal Access Token")
    tfs_project: str = Field(..., description="TFS project name")
    redis_url: Optional[str] = Field(
        None,
        description="Optional Redis URL passed to the DeerFlow skill as REDIS_URL; falls back to service REDIS_URL.",
    )
    human_feedback: Optional[list[dict]] = Field(
        None,
        description=("人工确认结果。格式：[{\"question_id\": \"q1\", \"answer\": \"...\"}, ...]。"
                     "传入后 Agent 据此进行第二轮工作流"),
    )


class TaskSubmitResponse(BaseModel):
    """Response returned after submitting an async task."""

    task_id: str = Field(..., description="Task ID for polling")
    status: str = Field("pending", description="Task status")


class TaskStatusResponse(BaseModel):
    """Polling response for an analysis task."""

    task_id: str = Field(..., description="Task ID")
    status: str = Field(..., description="pending/processing/completed/failed")
    error: Optional[str] = Field(None, description="Error message when failed")
    checklist: Optional[Dict[str, Any]] = Field(None, description="QC checklist when present")
    redis_key: Optional[str] = Field(None, description="Redis result key")
    redis_result: Optional[Dict[str, Any]] = Field(None, description="Full Redis result payload")
