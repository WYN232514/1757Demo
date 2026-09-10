# 192.168.23.248 前端部署

目标目录：

```text
/data/港铁受电弓/
└── 1757Demo/             # 本项目
```

历史工单原始图直接查询 Iceberg，不再要求服务器保存或挂载 `01_数据整理`。服务器必须能够访问 AWS Glue 和 S3；EC2 推荐绑定最小权限的只读 IAM Role。

部署完成后，内网用户访问：

```text
http://192.168.23.248:8050
```

如果配置 Nginx 反向代理，则直接访问：

```text
http://192.168.23.248
```

## 1. 在当前 Windows 电脑生成部署包

在 PowerShell 中执行：

```powershell
cd C:\Users\thinkpad\Desktop\1757Demo
.\prepare_server_bundle.ps1
```

生成文件：

```text
C:\Users\thinkpad\Desktop\1757Demo_server.tar.gz
```

部署包不会包含 `01_数据整理`、网关查询缓存、日志、Git 历史和 `.env`。`01_数据整理` 已不再是前端运行依赖；案例库、结构化工单、前端资源及程序代码会包含在内。

## 2. 上传文件

可以使用 WinSCP，以用户 `kcls` 登录 `192.168.23.248`，上传：

- `1757Demo_server.tar.gz` → `/data/港铁受电弓/`
- 当前项目的 `.env` → `/data/港铁受电弓/1757Demo/.env`（解压后再上传）

也可以在 PowerShell 使用 `scp`，命令会提示输入 `kcls` 的密码：

```powershell
scp C:\Users\thinkpad\Desktop\1757Demo_server.tar.gz "kcls@192.168.23.248:/data/港铁受电弓/"
```

## 3. 登录并解压

```bash
ssh kcls@192.168.23.248
cd '/data/港铁受电弓'
mkdir -p 1757Demo
tar -xzf 1757Demo_server.tar.gz -C 1757Demo
```

退出 SSH 后，在 Windows PowerShell 单独上传 `.env`（该文件没有包含在部署包中）：

```powershell
scp C:\Users\thinkpad\Desktop\1757Demo\.env "kcls@192.168.23.248:/data/港铁受电弓/1757Demo/.env"
```

也可以通过 WinSCP 上传，或在服务器上基于 `.env.example` 创建。必须包含原有网关凭据，并增加：

```dotenv
CASE_LIBRARY_DIR=/data/港铁受电弓/1757Demo/case_library
ICEBERG_RAW_ENABLED=true
AWS_REGION=ap-east-1
ICEBERG_WAREHOUSE=s3://mtr-inf-backup/R-Train/iceberg/fault_related_data
ICEBERG_DB=iceberg_db
ICEBERG_TABLE=fault_related_data
HISTORICAL_EVENT_CODE=1757
ICEBERG_QUERY_RETRIES=2
```

不要把 `批量下载.py` 或 `config.yaml` 中的长期 AWS 密钥上传到服务器。EC2 应使用 IAM Role；如果容器无法取得实例角色，需要把 EC2 的 IMDS hop limit 调整为 2，或者由管理员提供容器可用的标准 AWS 凭据注入方式。

设置权限：

```bash
chmod 600 '/data/港铁受电弓/1757Demo/.env'
```

## 4. 安装并启动

服务器需要 Python 3.10 或更高版本，并且能够创建 venv。先检查：

```bash
python3 --version
python3 -m venv --help >/dev/null
```

如果服务器能访问 Python 软件源：

```bash
cd '/data/港铁受电弓/1757Demo'
bash deploy/install_server.sh
```

脚本会创建 Linux 虚拟环境、安装依赖、检查页面、安装 systemd 服务并启动。

如果服务器不能联网，需要先在一台与服务器同架构、同 Python 版本的 Linux 电脑上准备离线依赖：

```bash
python3 -m pip download -r requirements-server.txt -d wheelhouse
```

将 `wheelhouse` 整体上传到 `/data/港铁受电弓/1757Demo/wheelhouse`，再执行安装脚本。Windows 虚拟环境不能直接复制到 Linux。

## 5. 检查服务

```bash
sudo systemctl status 1757demo
sudo journalctl -u 1757demo -f
curl -I http://127.0.0.1:8050
```

其他电脑访问：

```text
http://192.168.23.248:8050
```

如果服务器启用了防火墙，需要放行 TCP 8050。按服务器系统选择一条：

```bash
sudo firewall-cmd --permanent --add-port=8050/tcp
sudo firewall-cmd --reload
```

或：

```bash
sudo ufw allow 8050/tcp
```

## 6. 可选：通过 80 端口直接访问

服务器安装 Nginx 后：

```bash
sudo cp '/data/港铁受电弓/1757Demo/deploy/nginx-1757demo.conf' /etc/nginx/conf.d/1757demo.conf
sudo nginx -t
sudo systemctl reload nginx
```

之后访问：

```text
http://192.168.23.248
```

## 7. 后续更新代码

重新生成并上传部署包，覆盖解压后执行：

```bash
cd '/data/港铁受电弓/1757Demo'
.venv/bin/python -m pip install -r requirements-server.txt
sudo systemctl restart 1757demo
sudo journalctl -u 1757demo -n 100 --no-pager
```
