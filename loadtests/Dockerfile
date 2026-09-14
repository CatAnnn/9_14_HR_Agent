FROM python:3.11-slim

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /workspace
COPY loadtests/requirements-locust.txt /tmp/requirements.txt
RUN python -m pip install --no-cache-dir -r /tmp/requirements.txt
COPY loadtests /workspace/loadtests
RUN chmod 0755 /workspace/loadtests/entrypoint.sh
ENV PYTHONPATH=/workspace

ENTRYPOINT ["/workspace/loadtests/entrypoint.sh"]
