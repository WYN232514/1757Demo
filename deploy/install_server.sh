#!/usr/bin/env bash
set -euo pipefail

APP_DIR="/data/港铁受电弓/1757Demo"
DATA_DIR="/data/港铁受电弓/01_数据整理"
SERVICE_FILE="$APP_DIR/deploy/1757demo.service"

if [[ "$(id -un)" != "kcls" ]]; then
    echo "请使用 kcls 用户执行该脚本。" >&2
    exit 1
fi

cd "$APP_DIR"

python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else "Python 3.10 or newer is required.")'

if [[ ! -d "$DATA_DIR" ]]; then
    echo "数据目录不存在：$DATA_DIR" >&2
    exit 1
fi
if [[ ! -f "case_library/metadata.csv" ]]; then
    echo "案例库缺失：$APP_DIR/case_library/metadata.csv" >&2
    exit 1
fi
if [[ ! -f ".env" ]]; then
    echo "缺少 $APP_DIR/.env，请先从当前电脑安全复制该文件。" >&2
    exit 1
fi

grep -q '^DATA_DIR=' .env || printf '\nDATA_DIR=/data/港铁受电弓/01_数据整理\n' >> .env
grep -q '^CASE_LIBRARY_DIR=' .env || printf 'CASE_LIBRARY_DIR=/data/港铁受电弓/1757Demo/case_library\n' >> .env

python3 -m venv .venv
if [[ -d wheelhouse ]] && compgen -G 'wheelhouse/*' > /dev/null; then
    .venv/bin/python -m pip install --no-index --find-links wheelhouse -r requirements-server.txt
else
    .venv/bin/python -m pip install --upgrade pip
    .venv/bin/python -m pip install -r requirements-server.txt
fi

.venv/bin/python -m py_compile app_visualize.py gateway_download.py vcb_case_library_direct.py
.venv/bin/python - <<'PY'
import app_visualize as app
client = app.server.test_client()
assert client.get('/').status_code == 200
assert client.get('/_dash-layout').status_code == 200
print('应用导入及页面检查通过。')
PY

sudo install -m 0644 "$SERVICE_FILE" /etc/systemd/system/1757demo.service
sudo systemctl daemon-reload
sudo systemctl enable --now 1757demo.service
sudo systemctl --no-pager --full status 1757demo.service

echo
echo "部署完成：http://192.168.23.248:8050"
echo "实时日志：sudo journalctl -u 1757demo -f"
