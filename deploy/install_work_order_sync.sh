#!/usr/bin/env bash
set -euo pipefail

APP_DIR="${APP_DIR:-/opt/lq/1757Demo}"
COMPOSE_FILE="$APP_DIR/docker-compose.frontend.yml"

cd "$APP_DIR"
mkdir -p runtime/work_order_sync fault_extractor/fault_extractor/output case_library

if [[ "$(date +%z)" != "+0800" ]]; then
    echo "服务器时区不是 UTC+8；请先将系统时区设为 Asia/Shanghai，再安装定时器。" >&2
    echo "当前时间：$(date '+%Y-%m-%d %H:%M:%S %Z %z')" >&2
    exit 1
fi

if [[ ! -f .env ]]; then
    echo "缺少 $APP_DIR/.env" >&2
    exit 1
fi

required=(
    WORK_ORDER_WORKFLOW_URL
    WORK_ORDER_WORKFLOW_API_KEY
    WORK_ORDER_WORKFLOW_USER
    AWS_REGION
    ICEBERG_WAREHOUSE
    QDRANT_URL
    QDRANT_API_KEY
)
for name in "${required[@]}"; do
    if ! grep -q "^${name}=." .env; then
        echo ".env 缺少或未填写：${name}" >&2
        exit 1
    fi
done

sudo docker compose -f "$COMPOSE_FILE" config >/dev/null
sudo docker compose -f "$COMPOSE_FILE" build frontend updater
sudo docker compose -f "$COMPOSE_FILE" up -d --force-recreate frontend

sudo install -m 0644 deploy/work-order-sync.service /etc/systemd/system/work-order-sync.service
sudo install -m 0644 deploy/work-order-sync.timer /etc/systemd/system/work-order-sync.timer
sudo systemctl daemon-reload
sudo systemctl enable --now work-order-sync.timer

echo "定时更新已启用：每月 1 日和 16 日 02:30（Asia/Shanghai）。"
echo "查看下次执行：sudo systemctl list-timers work-order-sync.timer"
echo "立即测试：sudo systemctl start work-order-sync.service"
echo "查看日志：sudo journalctl -u work-order-sync.service -n 200 --no-pager"
