FROM vllm/vllm-openai:v0.21.0

ARG HTTP_PROXY
ARG HTTPS_PROXY
ARG NO_PROXY

ENV HTTP_PROXY=${HTTP_PROXY} \
    HTTPS_PROXY=${HTTPS_PROXY} \
    http_proxy=${HTTP_PROXY} \
    https_proxy=${HTTPS_PROXY} \
    NO_PROXY=${NO_PROXY} \
    no_proxy=${NO_PROXY} \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    MINERU_API_OUTPUT_ROOT=/tmp/mineru-output \
    MINERU_API_MAX_CONCURRENT_REQUESTS=2 \
    MINERU_TASK_RESULT_TIMEOUT_SECONDS=3600

RUN python3 -m pip install --no-cache-dir --upgrade pip \
    && python3 -m pip install --no-cache-dir "mineru[core]==3.4.5"

EXPOSE 7112
ENTRYPOINT []
CMD ["mineru-api", "--host", "0.0.0.0", "--port", "7112", "--enable-vlm-preload", "true", "--gpu-memory-utilization", "0.20"]
