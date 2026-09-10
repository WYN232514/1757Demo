# VCB/1757 故障分析 API

输入车号、车厢号、故障时间和故障码后，服务会自动：

1. 通过 Railiverse 外部接口下载故障前60秒、后30秒的数据；前端可在折叠的“下载区间”框中按秒调整，测点按案例库有效节点和目标车厢动态筛选，当前每次约28个；
2. 将接口返回的 JSON 时序数据转换为分析引擎使用的标准长表；
3. 生成当前故障的变化节点和向量；
4. 与本地历史案例库计算相似度；
5. 返回相似历史工单、相似度和相关说明。

当前网关请求使用 `equipModel`、`equipInstance`、`equipUnit` 参数。接口接收北京时间，返回 UTC 时间戳；程序会自动完成时区转换和按北京时间日期分区。

历史工单原始曲线不再读取或上传整日 `01_数据整理` CSV。点击历史工单时，前端会直接按 `ts_date + eventCode + equipInstance + UTC 时间窗口 + code` 从 Iceberg 查询绘图所需的小窗口数据，并在当前服务进程内缓存结果。新故障仍然从 Railiverse 网关实时下载。

“案例库工单”页不依赖新故障查询：页面启动后即列出案例库全部工单，支持按任意列筛选、排序和选择。选择工单后会自动显示结构化详情并加载故障前 60 秒、后 30 秒的原始数据图。Iceberg 首次查询结果同时写入 `runtime/historical_raw_cache/`；容器或服务重启后再次查看同一窗口会直接复用磁盘缓存。新故障查询完成后，案例库页会自动定位到 Top 1 相似工单。

11 个列车级变量（`132/133/145/160/161/236/237/252/253` 对应的 `..._01_sdr` code）固定按 `equipUnit=0` 处理；其中的 `01` 不是车厢号。VCB 和升弓完成等车厢级变量才会根据 `02/05/09` 映射。

## 服务器前端部署

前端 Docker 部署不再要求复制 `01_数据整理`。服务器需要能够访问 AWS Glue/S3；EC2 推荐绑定只读 IAM Role。部署步骤见 [DEPLOY_SERVER.md](DEPLOY_SERVER.md)。

## 案例库要求

运行中的前端只使用 Qdrant schema v2 完整案例库，不再读取服务器本地的 `vectors.npy`、`metadata.csv`、`node_index.joblib`、`segments.joblib`、工单 Excel/CSV 或结构化工单 JSON。原始时序数据仍来自 Railiverse 和 Iceberg/S3。

首次升级时需要用现有 `case_library/` 执行一次 `sync-qdrant` 迁移；迁移完成后，本地案例库文件只作为离线备份或临时构建输入，不是前端运行依赖。

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
ICEBERG_RAW_ENABLED=true
AWS_REGION=ap-east-1
ICEBERG_WAREHOUSE=s3://mtr-inf-backup/R-Train/iceberg/fault_related_data
ICEBERG_DB=iceberg_db
ICEBERG_TABLE=fault_related_data
HISTORICAL_EVENT_CODE=1757
ICEBERG_QUERY_RETRIES=2
```

`.env` 含有凭据，禁止提交到 Git 或发送给无关人员。Qdrant 是案例库唯一在线数据源，必须配置并可用。

生产服务器不要把 AWS Access Key 写进代码、镜像或 Git。优先使用 EC2 IAM Role；本地开发机才通过被 Git 忽略的 `config.yaml` 或标准 AWS 环境变量提供凭据。

## Qdrant 完整案例库

主 collection `vcb_case_library_v2` 的每个 case point 保存向量、metadata、segment、node_counts、工单正文、结构化工单详情和 Iceberg 定位信息；配套 collection `vcb_case_library_v2__meta` 保存 node_index、向量维数、schema 版本和案例库版本。使用新名称可以在首次迁移时保留旧 collection，便于回滚。

```dotenv
QDRANT_URL=http://10.0.1.87:6333
QDRANT_API_KEY=实际API Key
QDRANT_COLLECTION=vcb_case_library_v2
```

同步当前案例库：

```bash
python vcb_case_library_direct.py sync-qdrant \
  --library-dir case_library \
  --qdrant-url http://10.0.1.87:6333 \
  --qdrant-collection vcb_case_library_v2
```

同步会完整重建主 collection 和配套的 `__meta` collection。新版前端会校验 `schema_version=2`、案例数和向量维数；Qdrant 不可用或数据不完整时会拒绝启动，不会静默回退到过期本地文件。

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

### 前端登录认证

Dash 页面和回调默认要求登录。认证配置放在不会提交 Git 的 `.env.auth`：

```dotenv
FAULT_AI_LOGIN_USERNAME=faultAI
FAULT_AI_LOGIN_PASSWORD=请替换为强密码
FAULT_AI_SESSION_SECRET=请替换为至少32位随机字符串
FAULT_AI_COOKIE_SECURE=false
FAULT_AI_SESSION_HOURS=8
```

本地 HTTP 调试使用 `FAULT_AI_COOKIE_SECURE=false`；服务器通过 HTTPS 部署时必须设为
`true`。访问 `/mtr-1757/logout` 可退出登录。带有效 `X-API-Key` 的后端 POST
接口仍可独立调用，不依赖浏览器登录 Cookie。

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

相似工单响应结构：

```json
{
  "similar_work_orders": [
    {
      "rank": 1,
      "similarity": 0.88,
      "work_order": {
        "work_order_no": "5001603315",
        "case_id": "5001603315",
        "train": "T15",
        "car_no": "05",
        "fault_time": "2026-06-08 05:29:00",
        "remark": "P044 TMS VCB Trip Detected at MKK UT",
        "long_description": null,
        "structured_info": {
          "fault": "P044 VCB tripped",
          "evidence": "...",
          "component": "VCB, APC, ATR",
          "cause": "...",
          "action": "...",
          "result": "Function test passed",
          "phenomenon_component": ["VCB"],
          "phenomenon_events": ["tripped"],
          "cause_components": ["VCB control chain"],
          "cause_events": ["intermittent abnormality"],
          "treatment_components": ["VCB", "APC"],
          "treatment_events": ["checked", "function tested"],
          "work_order": "5001603315",
          "equip_model": "R-Train",
          "equip_instance": "T15",
          "equip_unit": "P044",
          "replace_in_part": [],
          "replace_out_part": []
        }
      }
    }
  ],
  "fault_report": {
    "available": true,
    "recommendation_source": "llm",
    "reference_work_order_count": 5,
    "missing_work_orders": [],
    "situation_assessment": ["..."],
    "recommended_steps": ["..."],
    "document_focus": ["..."],
    "cautions": ["..."],
    "synthesis_facts": {}
  }
}
```

`similarity` 是最终用于排序和阈值过滤的相似度。接口不返回原始相似度、细节相似度、选择规则、选择备注等内部计算字段。

`structured_info` 来自 `fault_extractor` 的结构化工单知识库。`fault_report` 基于当前相似工单生成排查与维修参考建议：模型调用成功时 `recommendation_source` 为 `llm`；模型不可用时自动使用有据可查的规则化建议并标记为 `deterministic_fallback`。

结构化工单与报告功能依赖以下本地文件：

```text
工单.xlsx
fault_extractor/fault_extractor/output/reference_data_with_cause_inferenced.json
fault_extractor/fault_extractor/llm_api_config.json
```

这些文件包含业务数据或模型凭据，不提交到 Git。部署到另一台电脑时必须通过安全方式单独复制。Docker 运行时，Compose 会将 LLM 配置只读挂载到容器中。

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
- 首次查询需要实时请求多个测点；后续相同窗口可复用缓存。
- 新故障下载默认每批最多请求 10 个测点（`GATEWAY_BATCH_SIZE=10`）；当前每个车厢筛选出约 28 个相关测点，通常拆分为 3 个批次，并以 `GATEWAY_DOWNLOAD_WORKERS=6` 并行下载。若网关出现 429/连接重置，可把并发降为 `3` 或 `4`。

## 工单案例库半月自动更新

`scheduled_work_order_sync.py` 每次完成以下链路：

1. 通过工作流执行 `SELECT * FROM WO_DETAIL`，按 `report_date` 查询已关闭/完成的 VCB 工单；
2. 将接口字段标准化，并按 `work_order_no + 内容哈希` 幂等去重；
3. 复用现有 extractor 提取故障时间和结构化工单信息；
4. 直接从 Iceberg 读取故障前后 30 分钟的 109 个测点，不再依赖 `01_数据整理`；
5. 复用现有 segment 和最终相似度向量算法更新案例库；
6. 从 Qdrant 旧库生成临时构建快照，完成合并后整库发布回 Qdrant，重启前端加载新版；临时快照任务结束即删除。

服务器 `.env` 必须包含以下配置，值不要加引号：

```env
WORK_ORDER_WORKFLOW_URL=http://43.199.48.66/v1/workflows/run
WORK_ORDER_WORKFLOW_API_KEY=app-替换为新Key
WORK_ORDER_WORKFLOW_USER=lq-123
```

首次部署或代码更新后执行：

```bash
cd /opt/lq/1757Demo
sudo docker compose -f docker-compose.frontend.yml build frontend updater
sudo docker compose -f docker-compose.frontend.yml run --rm \
  -v "$PWD/case_library:/migration/case_library:ro" \
  updater python vcb_case_library_direct.py sync-qdrant \
  --library-dir /migration/case_library
sudo docker compose -f docker-compose.frontend.yml up -d frontend
sudo bash deploy/install_work_order_sync.sh
```

上述 `sync-qdrant` 是从旧版升级到 schema v2 的一次性迁移，必须在启动新版前端前成功完成。以后定时更新直接以 Qdrant 旧库为基础，不依赖服务器本地 `case_library`。

定时器在每月 1 日和 16 日北京时间 02:30 执行。手动测试最近一个已经结束的半月区间：

```bash
sudo systemctl start work-order-sync.service
sudo journalctl -u work-order-sync.service -n 200 --no-pager
```

首次回补指定时间范围：

```bash
sudo docker compose -f docker-compose.frontend.yml run --rm updater \
  python scheduled_work_order_sync.py \
  --start "2026-07-01 00:00:00" \
  --end "2026-07-23 00:00:00"
sudo docker compose -f docker-compose.frontend.yml restart frontend
```

同步状态、失败重试队列和原始工作流响应保存在 `runtime/work_order_sync/`。查询使用一天重叠窗口，但已成功且内容未变化的工单不会重复建库；失败项会在下次任务自动重试。更新过程中前端继续使用旧案例库，只有完整链路成功后才重新加载。
