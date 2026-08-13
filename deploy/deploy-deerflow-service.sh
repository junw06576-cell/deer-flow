#!/usr/bin/env bash
# =============================================================================
# deploy-deerflow-service.sh
# deerflow-service 部署重启脚本（备份 → 解压 → rebuild → 重建容器 → 验证）
#
# 参考：docs/my-docs/192服务器DeerFlow运维手册.md 场景八
# 关键点：deerflow-service 是 Dockerfile build 镜像（非 bind mount），
#         改代码必须 rebuild 镜像 + --force-recreate 容器才生效。
#
# 用法（已登录服务器，无需 ssh 前缀）：
#   方式1：把 zip 放到 /opt/deer-flow/deploy/deerflow-service.zip，然后
#         bash /opt/deer-flow/deploy/deploy-deerflow-service.sh
#   方式2：自定义 zip 路径
#         bash /opt/deer-flow/deploy/deploy-deerflow-service.sh /tmp/deerflow-service.zip
# =============================================================================
set -euo pipefail

ZIP_PATH="${1:-/opt/deer-flow/deploy/deerflow-service.zip}"
DEPLOY_DIR="/opt/deer-flow/deerflow-service"
TS="$(date +%Y%m%d_%H%M%S)"
BACKUP_DIR="${DEPLOY_DIR}.bak.${TS}"
TMP_DIR="/opt/deer-flow/.dfs_deploy_${TS}"

# ---- 前置检查 ----
[ -f "$ZIP_PATH" ] || { echo "✘ zip 不存在: $ZIP_PATH"; echo "  用法: bash $0 <zip路径>"; exit 1; }
command -v unzip >/dev/null 2>&1 || { echo "✘ 服务器缺少 unzip，请先安装: yum install -y unzip 或 apt-get install -y unzip"; exit 1; }

# ---- 回滚钩子（任何步骤失败 → 还原原目录） ----
rollback() {
  echo ""
  echo "✘ 部署失败，执行回滚..."
  if [ -d "$DEPLOY_DIR" ]; then rm -rf "$DEPLOY_DIR"; fi
  if [ -d "$BACKUP_DIR" ]; then mv "$BACKUP_DIR" "$DEPLOY_DIR"; echo "  ✔ 已还原原目录: $DEPLOY_DIR"; fi
}
trap rollback ERR

echo "===== [0/6] 活跃任务提示 ====="
ACTIVE="$(docker exec deer-flow-redis redis-cli HLEN run:active 2>/dev/null || echo '?')"
echo "  当前活跃 run 数: ${ACTIVE}"
echo "  （仅重启 deerflow-service 不会中断 Gateway 中的 run；活跃 run 注册表在 Redis（run:active），重启后调度线程自动恢复）"

echo "===== [1/6] 备份原目录 ====="
mv "$DEPLOY_DIR" "$BACKUP_DIR"
echo "  ✔ 已备份到: $BACKUP_DIR"

echo "===== [2/6] 解压新代码 ====="
mkdir -p "$TMP_DIR"
unzip -q "$ZIP_PATH" -d "$TMP_DIR"
mv "$TMP_DIR" "$DEPLOY_DIR"
if [ ! -f "$DEPLOY_DIR/main.py" ] || [ ! -d "$DEPLOY_DIR/services" ]; then
  echo "  ✘ 解压结果缺关键文件（main.py/services），中止（触发回滚）"
  exit 1
fi
# 恢复服务器本地 .env（zip 不含敏感配置，避免覆盖服务器自有配置）
if [ -f "$BACKUP_DIR/.env" ]; then
  cp "$BACKUP_DIR/.env" "$DEPLOY_DIR/.env"
  echo "  ✔ 已从备份恢复 .env"
fi
echo "  ✔ 新代码已就位"

echo "===== [3/6] rebuild 镜像（只 build deerflow-service） ====="
cd /opt/deer-flow
docker compose --env-file .env -p deer-flow \
  -f docker/docker-compose.yaml \
  -f docker/docker-compose.dood.yaml \
  build deerflow-service

echo "===== [4/6] 重建容器 ====="
docker compose --env-file .env -p deer-flow \
  -f docker/docker-compose.yaml \
  -f docker/docker-compose.dood.yaml \
  up -d --force-recreate deerflow-service

echo "===== [5/6] 验证 ====="
sleep 3
docker logs deer-flow-service --tail 15
echo ""
echo "  ✔ 应看到 run poller started（调度线程已启动）"

echo "===== [6/6] 完成 ====="
echo ""
echo "部署完成。确认无误后删除备份:"
echo "  rm -rf \"$BACKUP_DIR\""
echo "验证任务: 提交一个需求分析，观察 docker logs -f deer-flow-service | grep -E \"COMPLETED|FAILED\""
