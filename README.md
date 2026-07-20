# VCB/1757 Fault Analysis API

这是用于 Docker 部署的最小运行目录。调用方提交车号、车厢号、故障时间和故障码，
服务自动从 Railiverse Gateway 下载故障窗口数据并返回相似历史工单。

## 启动

```powershell
Copy-Item .env.example .env
# 编辑 .env，填写真实网关参数和 API_KEY
docker compose up -d --build
```

服务地址：

- 健康检查：`http://127.0.0.1:8000/health`
- 接口文档：`http://127.0.0.1:8000/docs`
- 查询接口：`POST http://127.0.0.1:8000/api/v1/fault-analysis`

完整部署和调用示例见 [DEPLOY_API.md](DEPLOY_API.md)。

## 必要案例库

`case_library/` 中必须存在：

- `vectors.npy`
- `metadata.csv`
- `node_index.joblib`
- `segments.joblib`

这些文件通过 Docker volume 挂载，不会提交到 Git。将仓库部署到另一台机器时，需要通过
安全方式单独复制这四个文件。
