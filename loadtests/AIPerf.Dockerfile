FROM python:3.11-slim

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /workspace
COPY loadtests/requirements-aiperf.txt /tmp/requirements.txt
RUN python -m pip install --no-cache-dir -r /tmp/requirements.txt
COPY loadtests /workspace/loadtests
ENV PYTHONPATH=/workspace

ENTRYPOINT ["python", "-m", "loadtests.aiperf_matrix"]
