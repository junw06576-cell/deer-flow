import os
from dotenv import load_dotenv

load_dotenv()

DEERFLOW_BASE_URL = os.getenv("DEERFLOW_BASE_URL", "http://172.16.0.192:2026")
DEERFLOW_LANGGRAPH_URL = f"{DEERFLOW_BASE_URL}/api"
API_KEY = os.getenv("API_KEY", "tfs-buddy-secret-key")
DEER_FLOW_INTERNAL_AUTH_TOKEN = os.getenv("DEER_FLOW_INTERNAL_AUTH_TOKEN", "")
DEER_FLOW_OWNER_USER_ID = os.getenv("DEER_FLOW_OWNER_USER_ID", "auto-req-service")

AGENT_NAME = os.getenv("AGENT_NAME", "auto-analysis-agent")

# Redis 连接地址（为空则使用内存存储，仅适合单进程开发环境）
REDIS_URL = os.getenv("REDIS_URL", "")

# Redis QC 质控结果配置
REDIS_QC_KEY_PREFIX = "auto-req:qc:plan"
REDIS_QC_TTL_SECONDS = 604800  # 7 天


# 共享 Redis 客户端（惰性初始化）
_redis_client = None

def get_redis_client():
    """获取共享 Redis 客户端（无 REDIS_URL 时返回 None）"""
    global _redis_client
    if _redis_client is None and REDIS_URL:
        import redis
        _redis_client = redis.from_url(REDIS_URL, decode_responses=False)
    return _redis_client
