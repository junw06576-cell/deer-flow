#!/usr/bin/env bash
# pull-reg-auto-req-analysis.sh
# 从 TFS 仓库拉取 reg-auto-req-analysis skill 到 DeerFlow 服务器（纯拉取，绝不推送）
#
# 两种模式自动判断：
#   - 目标目录已是 git 仓库（remote 指向 TFS）→ 增量 pull（日常更新走这里）
#   - 目标目录不存在 / 不是 git 仓库 / remote 不对 → 挪走旧目录 + 全量 clone（首次或重置）
#
# 用法（PAT 已内置默认值，通常直接运行即可；如需覆盖见下）：
#   1. 直接运行：bash pull-reg-auto-req-analysis.sh
#   2. 覆盖 PAT：export TFS_PAT="其他PAT" 后运行，或 bash pull-reg-auto-req-analysis.sh "其他PAT"
set -uo pipefail

SKILL_DIR="/opt/deer-flow/skills/public/reg-auto-req-analysis-offline"
SKILL_NAME="$(basename "$SKILL_DIR")"
SKILLS_PUBLIC="$(dirname "$SKILL_DIR")"
# 备份目录必须放在 skills/public 之外：
#   DeerFlow 扫描 skills/public 时只排除「点开头」目录（local_skill_storage.py:83），
#   而 `${SKILL_DIR}.old.<ts>` 不以点开头 ⇒ 会被当正式 skill 收录，
#   且字符串排序在正式目录之后 ⇒ 按 name 去重时【归档版覆盖正式版】（必现劫持）。
BACKUP_ROOT="${BACKUP_ROOT:-/opt/deer-flow/backups}"
REPO_URL="http://tfs2018-web.winning.com.cn:8080/tfs/WinCode/Skill/_git/reg-auto-req-analysis-offline"

# ---- PAT：已写死默认，环境变量 TFS_PAT 或位置参数仍可覆盖 ----
PAT="${TFS_PAT:-}"
[ -z "$PAT" ] && [ $# -ge 1 ] && PAT="$1"
[ -z "$PAT" ] && PAT="dcfwxvnbtg66lkdtezus5l77yrrz5fqnzlsmwwhi2n3hp7izavma"

# TFS 2018 basic auth：直接注入 Authorization 头
# （credential store 的空用户名条目在 Linux git 上不兼容，会回退交互提示，故不用）
AUTH_B64="$(printf '%s' ":$PAT" | base64 -w0)"
gitc() { git -c http.extraHeader="AUTHORIZATION: Basic ${AUTH_B64}" "$@"; }

# ---- 热加载：以管理员身份登录后通知 Gateway 重新扫描最新 skill（不重启容器）----
# 背景：/api/skills/reload 是 admin-only 的 POST，受「CSRF 双重提交 + admin 角色」双重保护。
#   · 内部 token（X-DeerFlow-Internal-Token）角色为 internal，不满足 admin，调不动；
#   · 故采用「管理员登录拿 session + CSRF」方式（运维手册「方式B」修正版）。
#   管理员凭据可用环境变量 DEER_FLOW_ADMIN_EMAIL / DEER_FLOW_ADMIN_PASSWORD 覆盖，
#   未设置时回退到下方默认值。
hot_reload() {
  local BASE_URL="http://127.0.0.1:2026"
  local ADMIN_EMAIL="${DEER_FLOW_ADMIN_EMAIL:-506391157@qq.com}"
  local ADMIN_PASS="${DEER_FLOW_ADMIN_PASSWORD:-Admin@123}"
  local CJ="/tmp/deerflow_reload.cookies"
  rm -f "$CJ"

  # 1) 管理员登录
  #    注意：login/local 用 OAuth2PasswordRequestForm 解析，只认【表单】字段
  #    username / password（username 填邮箱），不认 JSON、也不认 email 字段名。
  local LOGIN_BODY
  LOGIN_BODY="$(curl -s -c "$CJ" \
    --data-urlencode "username=$ADMIN_EMAIL" \
    --data-urlencode "password=$ADMIN_PASS" \
    "$BASE_URL/api/v1/auth/login/local")"

  if ! grep -q "access_token" "$CJ"; then
    echo "  ⚠ 管理员登录失败，跳过热加载。"
    echo "    服务端响应：$LOGIN_BODY"
    echo "    （可能原因：密码错误 / 邮箱不在 config.yaml 的 auth.admin_emails / 服务不可达）"
    echo "    skill 文件已就位，待 Gateway 下次自然重载或人工处理即可。"
    rm -f "$CJ"
    return 0
  fi

  # 2) 取出 csrf_token（cookie jar 末列即 value）
  local CSRF
  CSRF="$(grep "csrf_token" "$CJ" | awk '{print $NF}')"
  if [ -z "$CSRF" ]; then
    echo "  ⚠ 未能获取 CSRF token，跳过热加载。"
    echo "    skill 文件已就位，待 Gateway 下次自然重载或人工处理即可。"
    rm -f "$CJ"
    return 0
  fi

  # 3) 带 session + CSRF 双重提交，触发 reload
  echo "===== [热加载] 通知 Gateway 重新扫描 skill ====="
  local RESP
  RESP="$(curl -s -b "$CJ" -H "X-CSRF-Token: $CSRF" -X POST "$BASE_URL/api/skills/reload")"
  if echo "$RESP" | grep -q '"success" *: *true'; then
    echo "  ✔ 热加载成功：$RESP"
  else
    echo "  ⚠ 热加载接口返回异常（$RESP）。skill 文件已就位，未自动重启 gateway，待 Gateway 下次自然重载或人工处理即可。"
  fi
  rm -f "$CJ"
}

# ---- 清理 skills/public 下的历史备份残留（会劫持同名正式 skill），两种模式都执行 ----
stale="$(ls -d "${SKILLS_PUBLIC}/${SKILL_NAME}".old.* 2>/dev/null || true)"
if [ -n "$stale" ]; then
  echo "===== [0/5] 清理 skills/public 下的历史备份残留 ====="
  echo "$stale" | while read -r d; do
    echo "  - 删除: $d"
    rm -rf "$d"
  done
  echo "  ✔ 残留已清（后续备份改放 ${BACKUP_ROOT}）"
fi

echo "===== [1/5] 探测远程仓库 ====="
if ! gitc ls-remote "$REPO_URL" >/dev/null 2>&1; then
  echo "  ✘ 远程仓库不可访问（认证失败或仓库不存在），中止"
  exit 1
fi
echo "  ✔ 远程仓库可访问"

echo "===== [2/5] 判断本地仓库状态 ====="
CUR_REMOTE="$(git -C "$SKILL_DIR" remote get-url origin 2>/dev/null || true)"
if [ -d "$SKILL_DIR/.git" ] && [ "$CUR_REMOTE" = "$REPO_URL" ]; then
  echo "  ✔ 已是 TFS git 仓库 → 增量 pull 模式"
  echo "===== [3/5] git pull ====="
  cd "$SKILL_DIR" || exit 1
  if gitc pull origin master; then
    echo "===== 完成 ====="
    git log --oneline -3
    hot_reload
    echo "（本脚本不执行任何 push，TFS 仓库不会被改动）"
    exit 0
  else
    echo "  ✘ pull 失败：可能本地有未提交修改，请先检查: git -C $SKILL_DIR status"
    exit 1
  fi
fi
if [ -d "$SKILL_DIR" ]; then
  echo "  ⚠ 目录存在但不是 TFS 仓库（remote=${CUR_REMOTE:-无}）→ 全量 clone 模式"
else
  echo "  - 目录不存在 → 全量 clone 模式"
fi

TS="$(date +%Y%m%d_%H%M%S)"
BACKUP_DIR="${BACKUP_ROOT}/${SKILL_NAME}.old.${TS}"

echo "===== [4/5] 挪走现有目录 ====="
mkdir -p "$BACKUP_ROOT"
if [ -d "$SKILL_DIR" ]; then
  mv "$SKILL_DIR" "$BACKUP_DIR"
  echo "  ✔ 已挪走: $BACKUP_DIR"
fi

echo "===== [5/5] clone 远程仓库 ====="
if ! gitc clone "$REPO_URL" "$SKILL_DIR"; then
  echo "  ✘ clone 失败"
  if [ -d "$BACKUP_DIR" ]; then
    rm -rf "$SKILL_DIR" 2>/dev/null
    mv "$BACKUP_DIR" "$SKILL_DIR"
    echo "  ↺ 已回滚到原目录"
  fi
  exit 1
fi
echo "  ✔ clone 完成"

echo "===== 验证 ====="
git -C "$SKILL_DIR" log --oneline -5
echo "  remote: $(git -C "$SKILL_DIR" remote get-url origin 2>/dev/null)"
echo "  顶层条目: $(ls "$SKILL_DIR" | tr '\n' ' ')"
echo ""

# ---- 备份保留策略 ----
# 仅在 clone + 验证成功之后执行：失败分支已 exit，备份一定还留着可供回滚。
# 默认保留最近 3 份（BACKUP_KEEP 覆盖，0 = 全删）。名字末段是 YYYYMMDD_HHMMSS，
# 字典序即时间序，所以 sort 后取头部之外的全部删掉。
KEEP="${BACKUP_KEEP:-3}"
echo "===== 备份保留（保留最近 ${KEEP} 份）====="
ls -1d "${BACKUP_ROOT}/${SKILL_NAME}".old.* 2>/dev/null | sort | head -n -"${KEEP}" | while read -r d; do
  echo "  - 清理旧备份: $d"
  rm -rf "$d"
done
echo "  ✔ 当前保留: $(ls -1d "${BACKUP_ROOT}/${SKILL_NAME}".old.* 2>/dev/null | wc -l) 份"

hot_reload
echo "完成。备份目录: ${BACKUP_DIR}（保留最近 ${KEEP} 份，BACKUP_KEEP=0 可全删）"
echo "（本脚本不执行任何 push，TFS 仓库不会被改动）"
