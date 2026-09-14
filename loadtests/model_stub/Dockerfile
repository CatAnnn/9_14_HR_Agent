FROM python:3.11-slim

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app
RUN python -m pip install --no-cache-dir fastapi==0.115.12 uvicorn==0.34.2
COPY loadtests /app/loadtests
ENV PYTHONPATH=/app

CMD ["uvicorn", "loadtests.model_stub.app:app", "--host", "0.0.0.0", "--port", "8099", "--workers", "1"]

