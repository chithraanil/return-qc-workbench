FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY qc_core/ qc_core/
COPY harness/ harness/
COPY mcp_server/ mcp_server/
COPY data/ data/
ENV PYTHONUNBUFFERED=1 QC_OUT_DIR=/tmp/qc_out
CMD exec uvicorn mcp_server.main:app --host 0.0.0.0 --port ${PORT:-8080} --workers 1
