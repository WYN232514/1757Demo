# VCB/1757 故障分析 API

输入车号、车厢号、故障时间和故障码后，服务会自动：

1. 通过 Railiverse 外部接口下载故障前后各 30 分钟的 109 个测点；
2. 将接口返回的 JSON 时序数据转换为分析引擎使用的标准长表；
3. 生成当前故障的变化节点和向量；
4. 与本地历史案例库计算相似度；
5. 返回相似历史工单、相似度和相关说明。

当前网关请求使用 `equipModel`、`equipInstance`、`equipUnit` 参数。接口接收北京时间，返回 UTC 时间戳；程序会自动完成时区转换和按北京时间日期分区。

## 目录要求

运行分析前，`case_library/` 必须包含以下四个文件：

```text
case_library/
├── vectors.npy
├── metadata.csv
├── node_index.joblib
└── segments.joblib
```

案例库、`.env`、下载数据、CSV、日志和缓存均被 Git 忽略。部署到另一台电脑时，需要单独安全复制 `.env` 和上述四个案例库文件。

## 环境配置

复制配置模板：

```powershell
Copy-Item .env.example .env
notepad .env
```

填写真实配置：

```env
RAILIVERSE_ENDPOINT=https://phm.railiverse.com/gateway/inte-api/open/external/param/list
RAILIVERSE_CLIENT_ID=实际客户端ID
RAILIVERSE_CLIENT_SECRET=实际客户端密钥
API_KEY=用于保护本服务的一段长随机值
QDRANT_URL=http://10.0.1.87:6333
```

`.env` 含有凭据，禁止提交到 Git 或发送给无关人员。当前查询流程使用本地案例库完成检索，不要求 Qdrant 在线可用。

## 方式一：Windows 原生运行

推荐 Python 3.12 或兼容版本。

```powershell
cd C:\Users\thinkpad\Desktop\1757Demo
python -m pip install -r requirements-api.txt
python -m uvicorn api_service:app --host 0.0.0.0 --port 8000 --workers 1
```

服务启动后保持窗口开启。`api_service.py` 会自动读取项目目录中的 `.env`。

另开一个 PowerShell 检查服务：

```powershell
Invoke-RestMethod http://127.0.0.1:8000/health
```

当 `status` 为 `ok`、`metric_count` 为 `109`、案例库缺失列表为空时，即可查询。

## 方式二：Docker 运行

```powershell
docker compose up -d --build
docker compose ps
docker compose logs -f fault-analysis-api
```

停止服务：

```powershell
docker compose down
```

## 完整故障查询

首次查询建议强制下载，并将超时设置得长一些：

```powershell
python test_api_call.py --train T25 --car-no 09 --fault-time "2026-05-18 21:25:52" --fault-code 1757 --threshold 0 --timeout 1800 --force-download --raw
```

参数说明：

- `--train`：车号，例如 `T25`；
- `--car-no`：车厢号，只能是 `02`、`05` 或 `09`；
- `--fault-time`：北京时间，格式为 `YYYY-MM-DD HH:MM:SS`；
- `--fault-code`：支持 eventCode（例如 `1757`）或 P 码（例如 `P026`）；
- `--threshold`：最低相似度，首次联调可使用 `0`，正式查询建议使用 `0.7`；
- `--force-download`：忽略同一窗口的本地缓存并重新下载；
- `--raw`：额外打印完整 JSON 响应。

`test_api_call.py` 会自动从 `.env` 读取 `API_KEY`。后续重复查询同一故障时，可以去掉 `--force-download` 复用缓存。

## HTTP 接口

- 健康检查：`GET http://127.0.0.1:8000/health`
- Swagger 文档：`GET http://127.0.0.1:8000/docs`
- 故障查询：`POST http://127.0.0.1:8000/api/v1/fault-analysis`

请求头：

```text
X-API-Key: 与 .env 中 API_KEY 相同的值
Content-Type: application/json
```

请求体示例：

```json
{
  "train": "T25",
  "car_no": "09",
  "fault_time": "2026-05-18 21:25:52",
  "fault_code": "1757",
  "top_k": 5,
  "similarity_threshold": 0.7,
  "time_is_cst": true,
  "force_download": false
}
```

下载后的标准数据保存在 `gateway_downloads/api_queries/`，主要列为：

```text
_time, _value, code, equipInstance, status, ts_date, eventCode
```

## 仅测试数据下载

不启动 API、只测试故障窗口下载：

```powershell
python download_data.py --train T25 --fault-time "2026-05-18 21:25:52" --fault-code 1757 --force
```

下载结果默认保存到 `gateway_downloads/queries/`。`download_original_env.py` 是按原始批量脚本改造的接口联调工具，不属于正式故障查询入口。

## 内网电脑离线安装依赖

如果内网电脑不能访问互联网，可在同为 Windows 64 位、相同 Python 版本的联网电脑执行：

```powershell
New-Item -ItemType Directory -Force wheelhouse
python -m pip download -r requirements-api.txt -d wheelhouse --only-binary=:all:
```

将项目、`wheelhouse/` 和 Python 安装程序复制到内网电脑，然后执行：

```powershell
python -m venv .venv
Set-ExecutionPolicy -Scope Process Bypass
.\.venv\Scripts\Activate.ps1
python -m pip install --no-index --find-links .\wheelhouse -r requirements-api.txt
```

随后按“Windows 原生运行”章节启动服务。

## 常见问题

- `WinError 10048`：8000 端口已有旧服务占用，先停止旧 Uvicorn 进程再启动；
- HTTP 400：请求体字段与接口协议不匹配，当前代码使用新版 `equipModel/equipInstance/equipUnit`；
- HTTP 403：当前网络或来源 IP 被网关/CloudFront 拦截；
- 单个测点无数据：程序会跳过并继续下载其他测点，只有全部测点为空才会失败；
- 首次查询较慢：正式流程默认逐测点请求以保证兼容性，后续相同窗口可复用缓存。
