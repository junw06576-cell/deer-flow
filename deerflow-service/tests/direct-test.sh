#!/bin/bash
# 直接调 Gateway LangGraph API，绕开 deerflow-service
# 用法：bash direct-test.sh

set -e

TID="direct-test-99999"
CSRF_TOKEN="direct-csrf-$(date +%s)"
GATEWAY="http://127.0.0.1:8001"
REDIS_KEY="auto-req:qc:plan:WN_Data_Platform:99999"

echo "=== Step 1: 创建 thread ==="
curl -s -X POST "$GATEWAY/api/threads" \
  -H "Content-Type: application/json" \
  -H "X-CSRF-Token: $CSRF_TOKEN" \
  -b "csrf_token=$CSRF_TOKEN" \
  -d '{
    "thread_id": "'$TID'",
    "assistant_id": "lead_agent",
    "metadata": {
      "source": "direct-test",
      "collection_name": "WN_Data_Platform",
      "work_item_id": 99999
    }
  }'
echo ""

echo "=== Step 2: 提交任务 ==="
# 构建和 deerflow-service 一样的 message
MSG='System automation task. Execute the requirement analysis/QC workflow using the agent'\''s configured requirement-analysis skill. Do not ask for clarification and do not stop after summarizing this input. Read the configured skill instructions, run the required workflow, and write the authoritative result to Redis using the provided redis_key. Return only a short JSON status summary when the workflow is finished.

Task input JSON:
{"action": "auto_req_analysis", "collection_name": "WN_Data_Platform", "work_item_id": 99999, "tfs_project": "test", "tfs_pat": "test", "redis_key": "'$REDIS_KEY'"}'

curl -s -X POST "$GATEWAY/api/threads/$TID/runs/wait" \
  -H "Content-Type: application/json" \
  -H "X-CSRF-Token: $CSRF_TOKEN" \
  -b "csrf_token=$CSRF_TOKEN" \
  -d '{
    "input": {
      "messages": [
        {"role": "user", "content": "'"$MSG"'"}
      ]
    },
    "context": {
      "agent_name": "auto-analysis-agent",
      "thread_id": "'$TID'",
      "non_interactive": true,
      "secrets": {
        "REDIS_URL": "redis://host.docker.internal:26380/0"
      }
    },
    "config": {
      "context": {
        "agent_name": "auto-analysis-agent",
        "thread_id": "'$TID'",
        "non_interactive": true,
        "secrets": {
          "REDIS_URL": "redis://host.docker.internal:26380/0"
        }
      },
      "recursion_limit": 500
    }
  }'
echo ""
echo "=== Step 3: 查 Redis ==="
sleep 2
docker exec deer-flow-redis redis-cli EXISTS "$REDIS_KEY"
