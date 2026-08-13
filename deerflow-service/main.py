from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from fastapi.middleware.cors import CORSMiddleware
from routers.analysis import router as analysis_router
from services.run_poller import start_poller
import os


@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用启动：拉起常驻调度线程，统一轮询所有活跃 Run 并收尾。"""
    start_poller()
    yield


app = FastAPI(
    title="DeerFlow Service",
    description="为 TFS-BUDDY 提供需求分析能力。当前场景：req-analysis（需求自动质控）。",
    version="2.0.0",
    lifespan=lifespan,
)

# CORS：允许本地 file:// 和所有来源的测试请求
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 需求质控路由
app.include_router(analysis_router)


# ── 测试页面 ──

_test_html_path = os.path.join(os.path.dirname(__file__), "api-test.html")


@app.get("/api-test", response_class=HTMLResponse)
async def api_test_page():
    """接口测试页面"""
    if os.path.exists(_test_html_path):
        with open(_test_html_path, encoding="utf-8") as f:
            return HTMLResponse(f.read())
    return HTMLResponse("<h1>api-test.html 文件不存在</h1>")


@app.get("/api/v1/health")
async def health_check():
    return {
        "status": "ok",
        "service": "deerflow-service",
        "version": "2.0.0",
        "endpoints": {
            "submit_analysis": "POST /api/v1/analysis",
            "poll_result": "GET /api/v1/analysis/{task_id}",
        },
    }
