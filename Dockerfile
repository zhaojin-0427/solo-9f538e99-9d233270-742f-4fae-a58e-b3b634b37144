FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    DATA_DIR=/data \
    PORT=8000 \
    MAX_DECOMP_BYTES=268435456

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app/ /app/

# 非 root 运行；/data 为数据卷（上传、结果、SQLite 登记库）
RUN useradd -r -u 10001 appuser && mkdir -p /data && chown appuser /data
USER appuser
VOLUME /data

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=3s --retries=3 \
  CMD python -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8000/healthz')" || exit 1

CMD ["python", "app.py"]
