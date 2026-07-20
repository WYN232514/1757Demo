# VCB/1757 故障分析 API 部署

## 1. 服务能力

调用方只需提交车号、车厢号、故障时间和故障码。服务会自动：

1. 从 Railiverse Gateway 下载该车组 109 个变量的故障前后各 30 分钟数据；
2. 生成与历史案例库相同口径的故障指纹；
3. 查询并返回相似历史工单及相关解释字段。

接口：`POST /api/v1/fault-analysis`。交互文档：`GET /docs`。

## 2. Docker 启动

先启动 Docker Desktop，并确认使用 Linux containers。然后在项目目录执行：

```powershell
Copy-Item .env.example .env
```

编辑 `.env`，填入真实网关地址、账号和一段足够长的 `API_KEY`，不要把 `.env`
提交给他人或放进镜像。随后启动：

```powershell
docker compose up -d --build
docker compose ps
docker compose logs -f fault-analysis-api
```

健康检查：

```powershell
Invoke-RestMethod http://127.0.0.1:8000/health
```

返回 `status: ok` 才表示案例库、109 变量配置和网关环境变量都已就绪。

## 3. 调用示例

PowerShell：

```powershell
$headers = @{ "X-API-Key" = "替换为 .env 中的 API_KEY" }
$body = @{
    train = "T9"
    car_no = "05"
    fault_time = "2026-07-16 10:10:00"
    fault_code = "P026"
    top_k = 5
    similarity_threshold = 0.7
    time_is_cst = $true
} | ConvertTo-Json

Invoke-RestMethod `
    -Method Post `
    -Uri "http://127.0.0.1:8000/api/v1/fault-analysis" `
    -Headers $headers `
    -ContentType "application/json" `
    -Body $body
```

通用 JSON 请求体：

```json
{
  "train": "T9",
  "car_no": "05",
  "fault_time": "2026-07-16 10:10:00",
  "fault_code": "P026",
  "top_k": 5,
  "similarity_threshold": 0.7,
  "time_is_cst": true
}
```

成功响应主要结构：

```json
{
  "ok": true,
  "request_id": "...",
  "request": {},
  "download": {
    "metric_count": 109,
    "row_count": 123456,
    "reused": false
  },
  "analysis": {
    "fault_time_utc": "...",
    "node_count": 20,
    "ramping_finish_status": "RF=1",
    "match_count": 5
  },
  "similar_work_orders": [
    {
      "rank": 1,
      "similarity": 0.88,
      "work_order": {
        "work_order_no": "5001614276",
        "case_id": "5001614276",
        "train": "T9",
        "car_no": "05",
        "fault_time": "2026-06-09 07:32:00",
        "remark": "...",
        "long_description": null,
        "structured_info": {
          "fault": "...",
          "evidence": "...",
          "component": "...",
          "cause": "...",
          "action": "...",
          "result": "..."
        }
      }
    }
  ],
  "fault_report": {
    "available": true,
    "recommendation_source": "llm",
    "situation_assessment": ["..."],
    "recommended_steps": ["..."],
    "document_focus": ["..."],
    "cautions": ["..."]
  }
}
```

## 4. 形成可调用链接

同一内网中，调用链接直接使用 Docker 主机 IP：

```text
http://<Docker主机IP>:8000/api/v1/fault-analysis
```

需要在 Windows 防火墙或服务器安全组中仅向调用方网段开放 TCP 8000。

如果需要跨公网调用，不建议直接暴露 8000。应在前面部署 Nginx/Caddy，并配置：

- 域名和 HTTPS 证书；
- 反向代理到 `127.0.0.1:8000`；
- `X-API-Key` 或企业网关认证；
- 来源 IP 白名单、超时和限流。

更稳妥的方式是让调用方通过公司 VPN/专线访问内网链接。

## 5. 数据与更新

- `case_library` 通过只读 volume 挂载，新增真实工单入库后，API 会在下一次请求时
  自动检测文件变化并重载，不需要重启容器。
- 网关查询缓存挂载到 `gateway_downloads/docker`，相同车号和时间窗口会复用缓存。
- API 查询使用本地 `case_library` 完成当前项目的最终相似度算法，不依赖 Qdrant
  在线可用；Qdrant 仍用于现有入库同步和其他检索流程。

更新代码或依赖后重新构建：

```powershell
docker compose up -d --build
```

停止服务：

```powershell
docker compose down
```
