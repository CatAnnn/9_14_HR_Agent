# Fish S2-Pro 八路并发吞吐实测（2026-09-06）

> 本文保留首轮八路测试结果与当时的生效状态；后续十路调优、十二路播放失败及最新配置见 [继续调优报告](tts-throughput-tuning-20260906.md)。

## 结论

在当前独占 GPU 1 的 48 GiB RTX 4090 上，将 Fish 两个阶段的 `max_num_seqs` 从 4 提高到 8，保持模型、权重、音色、采样参数和 DAC 分组上限不变。预热后三轮长文本实测，总音频吞吐从 **5.978 提升至 10.759 音频秒/墙钟秒，约提高 80%**。这是总吞吐提升，不是单个用户回复提速 80%。

八路真实模型请求和八路应用内模拟用户接收测试均通过；八路配置下额外复测四用户也通过。**八路是本次已验证的并发，不是模型理论上限，也不等于八台浏览器的完整网站验收。**

## 配置与环境

- 模型：`fishaudio/s2-pro`，权重 revision `1de9996b6be38b745688de084d87a5633f714e4e`。
- 运行镜像：`06-fish-tts-vllm-omni:0.24.0-s2-pro`；沿用项目锁定的 vLLM / vLLM-Omni 0.24.0、Fish Speech 0.1.0、PyTorch 2.11.0+cu130，没有升级或更换模型。
- GPU：GPU 1，NVIDIA GeForce RTX 4090，驱动报告总显存 49,140 MiB（本机为 48 GiB 版本，并非通常的 24 GiB 卡）；驱动 595.84。其他模型在 GPU 0。
- 音色：注册的 `employee-natural`，使用固定种子生成的合成参考音频，不使用真实用户音频。
- 最终模型参数：Stage 0 = 8、Stage 1 = 8；DAC padded frames cap = 256、DAC FP16、常规 codec chunk = 25，均未进一步扩大或更改。
- 应用配置：`TTS_MAX_CONCURRENCY=8`、`TTS_GLOBAL_MAX_CONCURRENCY=8`；保留原有分句、独立播放反馈、取消释放和跨后端全局限流逻辑。

实际模型配置来源是 [Fish YAML](../fish_tts/config/fish_s2_pro_dynamic_batch.yaml)。`TTS_STAGE_*` 是应用侧状态展示配置，不代替模型 YAML。八条活跃流不要求一次 DAC forward 同时解码全部八条。

## 直接模型实测

测试使用 [182 字合成长文本](../loadtests/data/tts/sustained-reply.zh.txt)，每个请求追加独立编号，使用 `41000 + request_index` 种子。原始文本文件 SHA-256：`4f444db766a567ba6b4bedb62cbd52a5576aa367350781dad5091c1b2fa6cf9f`；JSON 中的 `text_sha256` 对应去掉首尾空白后的正文，为 `caad194dc6e38cee6fbf9275f8f7b00ecb1db8b85277a1835a930035639a6ed7`。各配置先进行不计分的短文本预热，再执行三轮并发请求。

直连长文本使用 `max_new_tokens=2048`；网站按句合成仍使用原来的 1024 上限。这两类测试工作负载不能混算吞吐。

| 两阶段容量 / 并发请求 | 成功请求 | 总音频吞吐（音频秒/墙钟秒） | 最差单轮首音 P95 | 每轮最长共同模拟播放 | 模拟播放中断 |
|---|---:|---:|---:|---:|---:|
| 4 / 4 | 12/12 | 5.978 | 0.349 秒 | 31.60–31.63 秒 | 0 |
| 6 / 6 | 18/18 | 7.946 | 0.755 秒 | 32.37 秒 | 0 |
| 8 / 8 | 24/24 | 10.759 | 0.844 秒 | 31.69–31.90 秒 | 0 |

总吞吐按三轮合计音频时长除以合计墙钟时间计算，而非简单取吞吐均值。八路相对四路提升 `79.956%`。首音指 HTTP 请求开始到第一份模型 PCM 到达，不包含 LLM、网站业务排队和浏览器起播缓冲；样本很少，表中的 P95 不代表长期生产分位数。

播放模拟根据 HTTP PCM 到达时间，使用当前前端 1.4 秒起播/重缓冲规则。验收要求所有请求成功、单次中断不超过 0.1 秒、所有用户最长共同连续播放不少于 10 秒。

对照实验：**旧的四路模型容量接收六个同时请求**，虽然 6/6 最终返回且模拟播放没有中断，首音 P95 却达到 **20.94 秒**，有两人等待约 20 秒。仅放大应用入口名额、或者仅看“零中断”，都不能证明多用户体验合格。

### GPU 观测

以下仅统计各组三轮直连测试时间窗，每秒采样：

| 并发请求 | GPU 1 显存 | GPU 1 平均利用率 | 最高温度 |
|---|---:|---:|---:|
| 4 | 23,763 MiB | 94.20% | 59°C |
| 6 | 23,985 MiB | 87.42% | 63°C |
| 8 | 23,977 MiB | 94.12% | 60°C |

八路相对四路显存增加 214 MiB。上述窗口 GPU 0 均为 26,401 MiB、采样利用率为 0%；没有观察到其负载变化，但这**不证明 ASR、embedding、reranker 在繁忙时仍完全不受影响**。

## 应用内多用户与取消验证

另用真实 `FishTtsService`、分句、进程内并发名额、逐流播放反馈和 `SpeechWebSocketHub`，连接真实模型 HTTP 服务。接收端为八个独立的内存模拟客户端，不是网络浏览器。

| 模型容量 / 模拟用户 | 正常播放 | 最长共同模拟播放 | 取消隔离 |
|---|---|---:|---|
| 4 / 4 | 全部完成 | 17.55 秒 | 通过 |
| 6 / 6 | 全部完成 | 17.51 秒 | 通过 |
| 8 / 8 | 全部完成 | 17.49 秒 | 通过 |
| 8 / 4（回归） | 全部完成 | 17.43 秒 | 通过 |

取消验收要求：取消时其余用户仍在运行，取消后仍分别收到新增 PCM，并最终成功完成；不是只检查任务没有报错。测试还核验各接收端 PCM 哈希与其目标流匹配、缓冲反馈等待发生在占用合成名额之前。

这层测试不包含 Redis、数据库、完整业务路由、LLM、真实 WebSocket 网络和浏览器。PCM 格式与哈希不能证明语义朗读完整、无静音或音质自然，仍需真实设备抽样试听或转写对照。

最终后端回归 **305 项全部通过（14.04 秒）**，覆盖基准脚本、四用户播放、缓冲反馈、分句、取消、语音连接、分布式协调、配置和相关工作流；`docker compose --profile tts config --quiet` 及相关文件的 `git diff --check` 通过。这些自动化回归不能替代上面的真实 GPU 测试或完整浏览器验收。

## 复现与原始记录

主机侧直连示例（先完成模型启动、固定音色注册及预热）：

```bash
python scripts/benchmark_fish_tts.py \
  --concurrency 8 --rounds 3 \
  --text-file loadtests/data/tts/sustained-reply.zh.txt \
  --max-new-tokens 2048 \
  --playback-buffer-seconds 1.4 \
  --max-playback-gap-seconds 0.1 \
  --min-common-playback-seconds 10 \
  --require-continuous-playback \
  --output loadtests/results/fish-eight-user.json
```

安装了项目依赖的环境中执行应用内测试；在后端容器中将 URL 换为 `http://fish_tts:8091/v1/audio/speech`：

```bash
python -m scripts.benchmark_fish_pipeline \
  --url http://127.0.0.1:7119/v1/audio/speech \
  --concurrency 8 --slots 8 --scenario both \
  --min-common-playback-seconds 10 \
  --require-continuous-playback \
  --output loadtests/results/fish-pipeline-eight-user.json
```

本次原始文件位于本机 `loadtests/results/fish-20260906.v0Ypq7/`，该目录被 Git 忽略，不随代码提交：

- `manifest.json`、`a4-model.yaml`、`a6-model.yaml`、`a8-model.yaml`：初始环境与模型配置快照。
- `a4-long-c4.json`、`a6-long-c6.json`、`a8-long-c8.json`：三组正式直连结果。
- `a4-long-c6.json`：旧模型容量的排队对照。
- `a4-pipeline-c4.json`、`a6-pipeline-c6.json`、`a8-pipeline-c8.json`、`a8-pipeline-c4.json`：应用内播放及取消结果。
- `*-warm-*.json`：不计入正式结果的预热记录。
- `gpu.csv`：GPU 采样，时间为 Asia/Shanghai；直连 JSON 的 UTC 时间加八小时后对齐。

## 当前生效状态与后续边界

- Fish 模型容器已以 **8 / 8** 配置运行，健康检查通过；固定权重、镜像与合成音色缓存保留。
- `.env`、配置默认值及 Compose 的应用并发名额已同步为 8。**现有两个后端容器未重建，其启动环境仍是 4；下一次正常重建后端才会使用新的名额。** 不因本次测试重启其他业务服务。
- **用户的 `TTS_ENABLED=false` 保持不变，未向全站开启语音。** 应用内测试只在测试进程启用 TTS，不修改此总开关。
- 已停止 GPU 采样，并清理本次成功退出的一次性权重下载容器；模型卷、音色卷与镜像未删除。
- 本次是固定文本、固定音色、预热后的短时测试，未执行长时稳定性或容量上限探索。

下一阶段应在明确开启网站语音后，用独立账号/会话做真实浏览器多用户验收，记录发送到实际起播时间、长回复续句、网络抖动、取消/重连，并加入同时录音与文字生成负载。具体检查项见 [四用户播放验收](tts-four-user-playback.md)。
