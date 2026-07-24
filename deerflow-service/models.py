from pydantic import BaseModel, Field
from typing import Optional, Dict, Any


class AnalysisRequest(BaseModel):
    """提交需求质控任务"""
    collection_name: str = Field(..., description="TFS 集合名称（如 WN_Data_Platform）")
    work_item_id: int = Field(..., description="TFS 工作项 ID")
    tfs_pat: str = Field(..., description="TFS Personal Access Token")
    tfs_project: str = Field(..., description="TFS 项目名称")


class TaskSubmitResponse(BaseModel):
    """任务提交响应"""
    task_id: str = Field(..., description="任务 ID，用于轮询结果")
    status: str = Field("pending", description="任务状态")


class TaskStatusResponse(BaseModel):
    """任务状态轮询响应（completed 时携带 checklist）"""
    task_id: str = Field(..., description="任务 ID")
    status: str = Field(..., description="任务状态：pending/processing/completed/failed")
    error: Optional[str] = Field(None, description="错误信息（仅 failed 时有值）")
    checklist: Optional[Dict[str, Any]] = Field(None, description="质控 checklist（仅 completed 时有值）")
