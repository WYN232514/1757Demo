FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    CASE_LIBRARY_DIR=/app/case_library \
    GATEWAY_METRICS_JSON=/app/gateway_metrics.json \
    GATEWAY_QUERY_ROOT=/data/gateway_queries \
    NO_PROXY=localhost,127.0.0.1,10.0.1.87,::1 \
    no_proxy=localhost,127.0.0.1,10.0.1.87,::1

WORKDIR /app

COPY requirements-api.txt ./
RUN pip install --no-cache-dir -r requirements-api.txt

COPY api_service.py app_visualize.py gateway_download.py vcb_case_library_direct.py ./
COPY gateway_metrics.json ./
COPY fault_extractor/fault_extractor ./fault_extractor/fault_extractor

RUN mkdir -p /app/case_library /data/gateway_queries

EXPOSE 8000
VOLUME ["/app/case_library", "/data/gateway_queries"]

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import json,urllib.request; data=json.load(urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=3)); raise SystemExit(0 if data.get('status') == 'ok' else 1)"

CMD ["uvicorn", "api_service:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
