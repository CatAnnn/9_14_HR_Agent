# Qwen3 Reranker 原生评分服务

`qwen_reranker` 固定使用 vLLM `v0.27.1`，按官方 Qwen3 示例把原始
`Qwen/Qwen3-Reranker-4B` 以 sequence-classification pooling runner 加载。后端通过
`RERANK_LOCAL_PROTOCOL=rerank` 调用原生 `/v1/rerank`。

生产 Embedding 仍基于 vLLM `v0.8.5`，并通过
`Dockerfile.v0.8.5-no-xet` 移除不兼容的旧版 `hf-xet`。Reranker 使用独立的
vLLM 缓存 volume，避免与 Embedding 的 0.8.5 编译缓存混用；两者仍共享
Hugging Face 模型缓存。

生产服务保持宿主机端口 `7116`：

```bash
curl -sS http://127.0.0.1:7116/v1/rerank \
  -H 'Content-Type: application/json' \
  -d '{"model":"Qwen/Qwen3-Reranker-4B","query":"绩效反馈","documents":["绩效反馈应基于事实。","今天天气晴朗。"],"top_n":2}'
```

`qwen_reranker_shadow` 保留为独立配置验证服务，属于 `rerank-shadow`
profile，默认不会启动；端口仅绑定宿主机
`127.0.0.1:7123`：

```bash
docker compose --profile rerank-shadow up -d qwen_reranker_shadow
curl -sS http://127.0.0.1:7123/v1/rerank \
  -H 'Content-Type: application/json' \
  -d '{"model":"Qwen/Qwen3-Reranker-4B","query":"绩效反馈","documents":["绩效反馈应基于事实。","今天天气晴朗。"],"top_n":2}'
docker compose --profile rerank-shadow stop qwen_reranker_shadow
```

`benchmark_reranker_shadow.py` 仍可用于与单独部署的 legacy completion
端点比较（默认不加 `--run` 时不会发出网络请求）。生产 `7116`
已是 native rerank，不能再作为该脚本的 legacy endpoint。

```bash
python scripts/benchmark_reranker_shadow.py --help
```

影子服务默认使用物理 GPU 1。当前 GPU 1 若出现 NVML `Unknown Error`，应先修复
GPU/驱动再启动；不要把它改绑到 GPU 0。GPU 0 承载 Embedding/ASR，改绑会造成
显存争用，也会破坏新旧 reranker 吞吐 A/B 的隔离条件。本地生产 reranker 正在
使用 GPU 1 时，也不要同时启动影子服务。

启动参数和 chat template 对齐 vLLM `v0.27.1` 官方示例：

- https://github.com/vllm-project/vllm/releases/tag/v0.27.1
- https://github.com/vllm-project/vllm/blob/v0.27.1/examples/pooling/score/qwen3_reranker_online.py
- https://github.com/vllm-project/vllm/blob/v0.27.1/examples/pooling/score/template/qwen3_reranker.jinja
