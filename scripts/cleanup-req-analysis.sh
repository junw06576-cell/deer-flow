#!/usr/bin/env bash
set -euo pipefail

# ============================================================================
# 需求分析进程彻底清理脚本
#
# 把一个 TFS 需求分析(thread_id = {collection}-{workItemId})的所有残留清干净：
#   ① cancel 正在跑/挂起的 run（释放内存 task）
#   ② DELETE thread（删 thread 目录 + checkpoint + thread_meta）
#   ③ 补删 DELETE /api/threads 漏掉的 3 处：
#       - sqlite runs / feedback 表记录（宿主机直接删）
#       - 报告过程文件 /opt/deer-flow/auto-dev-work/过程文件/{workItemId}/
#       - Redis QC plan key  auto-req:qc:plan:{collection}:{workItemId}
#
# 用法：
#   ./cleanup-req-analysis.sh <collection> <workItemId>          # 预览(dry-run，只读不删)
#   ./cleanup-req-analysis.sh <collection> <workItemId> --yes    # 真正执行
# ============================================================================

# ---- 可调参数 ----
GATEWAY="deer-flow-gateway"
REDIS="deer-flow-redis"
OWNER="tfs-buddy"
DB="/opt/deer-flow/backend/.deer-flow/data/deerflow.db"
REPORT_BASE="/opt/deer-flow/auto-dev-work/过程文件"
REDIS_PASS="92e3ffa5db03aca31594021ddbf3c466"
# DELETE /threads 是同步 rmtree，大 thread 目录可能数分钟；按需调大
REQ_TIMEOUT="${REQ_TIMEOUT:-900}"
THREAD_BASE="/opt/deer-flow/backend/.deer-flow"

COLLECTION="${1:-}"
WID="${2:-}"
FLAG="${3:-}"

if [[ -z "$COLLECTION" || -z "$WID" ]]; then
  echo "用法: $0 <collection> <workItemId> [--yes]"
  echo "示例: $0 WN_Data_Platform 242042 --yes"
  exit 1
fi

# 防注入/路径穿越：只允许字母数字下划线连字符
if [[ "$COLLECTION" =~ [^A-Za-z0-9_-] || "$WID" =~ [^A-Za-z0-9_-] ]]; then
  echo "❌ collection / workItemId 只能含字母、数字、下划线、连字符"
  exit 1
fi

TID="${COLLECTION}-${WID}"
QC_KEY="auto-req:qc:plan:${COLLECTION}:${WID}"
DO_IT=0
[[ "$FLAG" == "--yes" ]] && DO_IT=1

echo "==================== 需求分析彻底清理 ===================="
echo "thread_id : $TID"
echo "sqlite    : $DB"
echo "报告目录   : ${REPORT_BASE}/${WID}/"
echo "Redis key  : $QC_KEY"
if [[ $DO_IT -eq 1 ]]; then echo "模式       : 执行"; else echo "模式       : 预览(dry-run，只读不删)"; fi
echo "==========================================================="

TOKEN="$(docker exec "$GATEWAY" printenv DEER_FLOW_INTERNAL_AUTH_TOKEN 2>/dev/null || true)"
if [[ -z "$TOKEN" ]]; then
  echo "❌ 拿不到 token（若 .env 未设置，运行时 token 随机生成，无法外部获取）"
  echo "   排查: docker exec $GATEWAY printenv | grep -i internal"
  exit 1
fi

# ---- 容器内：查 runs → cancel → 删 thread ----
# 注意：API 阶段失败（超时/报错）不能中断后面的宿主机清理，否则 sqlite/报告/Redis 全漏删
API_OK=1
docker exec -i \
  -e TID="$TID" -e TOKEN="$TOKEN" -e OWNER="$OWNER" -e DO_IT="$DO_IT" -e REQ_TIMEOUT="$REQ_TIMEOUT" \
  "$GATEWAY" python3 - <<'PY' || API_OK=0
import os, json, secrets, time, urllib.request, urllib.error

TID   = os.environ["TID"]
TOKEN = os.environ["TOKEN"]
OWNER = os.environ["OWNER"]
DO_IT = os.environ["DO_IT"] == "1"
BASE  = "http://localhost:8001/api"

# CSRF 是 double-submit 模式：cookie 与 header 同值即可通过比对（不校验真伪）
_csrf = secrets.token_urlsafe(32)
# DELETE thread 内部是同步 shutil.rmtree(thread_dir)（threads.py:485 未走线程池），
# 大 thread 目录在 overlayfs 上可能要数分钟，超时必须放宽。
TIMEOUT = int(os.environ.get("REQ_TIMEOUT", "900"))

def req(method, path):
    headers = {
        "X-DeerFlow-Internal-Token": TOKEN,
        "X-DeerFlow-Owner-User-Id": OWNER,
        "X-CSRF-Token": _csrf,
        "Cookie": f"csrf_token={_csrf}",
    }
    r = urllib.request.Request(BASE + path, method=method, headers=headers)
    try:
        with urllib.request.urlopen(r, timeout=TIMEOUT) as resp:
            return resp.status, resp.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()

code, raw = req("GET", f"/threads/{TID}/runs")
runs = []
if code == 200 and raw.strip():
    try:
        runs = json.loads(raw)
    except Exception:
        runs = []
print(f"[1] GET runs -> HTTP {code}，共 {len(runs)} 条")

active = [r for r in runs if r.get("status") in ("pending", "running")]
for r in runs:
    tag = "   <-- 活动，将被 cancel" if r in active else ""
    print(f"    - {r.get('run_id')}  status={r.get('status')}{tag}")

if not DO_IT:
    print(f"\n[预览] 将 cancel 的活动 run: {len(active)} 个")
    print("[预览] 将 DELETE thread")
    raise SystemExit(0)

for r in active:
    rid = r["run_id"]
    c, _ = req("POST", f"/threads/{TID}/runs/{rid}/cancel?action=interrupt&wait=true")
    print(f"[2] cancel {rid} -> HTTP {c}")

_t0 = time.time()
c, body = req("DELETE", f"/threads/{TID}")
print(f"[3] DELETE thread -> HTTP {c}  耗时 {time.time()-_t0:.1f}s  {body[:120]}")
PY

# ---- 宿主机：删 sqlite runs/feedback + 报告目录 + Redis QC plan ----
if [[ $DO_IT -eq 1 ]]; then
  # [4] sqlite runs + feedback 表记录
  if command -v sqlite3 >/dev/null 2>&1; then
    sqlite3 "$DB" "DELETE FROM feedback WHERE thread_id='$TID'; DELETE FROM runs WHERE thread_id='$TID';"
    echo "[4] sqlite 删除完成（sqlite3）: $DB"
  elif command -v python3 >/dev/null 2>&1; then
    python3 - "$DB" "$TID" <<'PY'
import sqlite3, sys
db, tid = sys.argv[1], sys.argv[2]
c = sqlite3.connect(db)
c.execute("DELETE FROM feedback WHERE thread_id=?", (tid,))
c.execute("DELETE FROM runs WHERE thread_id=?", (tid,))
c.commit()
print("deleted rows:", c.total_changes)
c.close()
PY
    echo "[4] sqlite 删除完成（python3）: $DB"
  else
    echo "[4] ❌ 宿主机无 sqlite3 也无 python3，请手动删 $DB 中 thread_id='$TID' 的 runs + feedback 记录"
  fi

  # [5] 报告目录
  REPORT_DIR="${REPORT_BASE}/${WID}"
  if [[ -d "$REPORT_DIR" ]]; then
    rm -rf "$REPORT_DIR"
    echo "[5] 已删报告目录: $REPORT_DIR"
  else
    echo "[5] 报告目录不存在(跳过): $REPORT_DIR"
  fi

  # [6] Redis QC plan
  docker exec "$REDIS" redis-cli -a "$REDIS_PASS" DEL "$QC_KEY" 2>/dev/null
  echo "[6] 已删 Redis QC key: $QC_KEY"

  # [7] 宿主机 thread 目录兜底
  # API 超时时容器内 rmtree 可能仍在跑、也可能没跑完；这里在宿主机上直接清干净
  for d in "${THREAD_BASE}"/threads/"${TID}" "${THREAD_BASE}"/users/*/threads/"${TID}"; do
    if [[ -d "$d" ]]; then
      echo "[7] 兜底删 thread 目录: $d  大小=$(du -sh "$d" 2>/dev/null | cut -f1)"
      rm -rf "$d"
    fi
  done
  echo "[7] thread 目录兜底清理完成"

  if [[ $API_OK -eq 0 ]]; then
    echo "⚠️  API 阶段失败（多数是 DELETE 超时），宿主机清理已继续；"
    echo "    若 thread_meta/checkpoint 仍残留，重跑一次本脚本即可（幂等）"
  fi

  echo ""
  echo "---- 验证 ----"
  if command -v sqlite3 >/dev/null 2>&1; then
    echo "    sqlite runs 剩余: $(sqlite3 "$DB" "SELECT COUNT(*) FROM runs WHERE thread_id='$TID';")"
  elif command -v python3 >/dev/null 2>&1; then
    python3 - "$DB" "$TID" <<'PY'
import sqlite3, sys
db, tid = sys.argv[1], sys.argv[2]
try:
    c = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    print("    sqlite runs 剩余:", c.execute("SELECT COUNT(*) FROM runs WHERE thread_id=?", (tid,)).fetchone()[0])
except Exception as e:
    print("    sqlite 校验失败:", e)
PY
  fi
  for d in "${THREAD_BASE}"/threads/"${TID}" "${THREAD_BASE}"/users/*/threads/"${TID}"; do
    [[ -d "$d" ]] && echo "    ⚠️ 仍存在 thread 目录: $d"
  done
  echo "    报告目录: $([[ -d "${REPORT_BASE}/${WID}" ]] && echo '⚠️ 仍存在' || echo '已清理')"
else
  echo ""
  echo "[预览] 将删除 sqlite runs/feedback 记录（thread_id=$TID）"
  echo "[预览] 将删除报告目录: ${REPORT_BASE}/${WID}/"
  echo "[预览] 将删除 Redis key: $QC_KEY"
fi

echo ""
echo "完成。"
