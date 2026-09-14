# 四人同时播放语音：实现与验收

## 目标和边界

目标是四个独立用户、四个不同会话同时持续听到各自的员工语音，不串音，不因一人停止而影响其他三人。
同一会话的新语音连接仍会替换旧连接；四个标签页复用同一个会话不是四用户测试。

初始模型 Stage 0 / Stage 1、单后端及跨后端合成容量的基线是四路。2026-09-06 首轮测试提高为八路，后续三轮分句播放验证后采用十路；四用户仍是必须保留的回归目标，详见 [首轮吞吐报告](tts-throughput-20260906.md) 和 [继续调优报告](tts-throughput-tuning-20260906.md)。实验保持相同模型、权重版本、音色和采样参数；DAC 512 无实测收益、十二路出现重缓冲，均未采用。
四路合成能力不自动等于四路持续播放能力，最终必须以真实音频的产生速度、首音延迟和播放中断验收。

## 本次实现

- 首段允许在自然短语边界提前发送，不等待长句全部完成；后续只合并已经到达的短句，不额外等 LLM 凑字数。
- 只改变 TTS 分段，不截短、改写或限制员工回复内容。
- 浏览器按独立 stream 回传缓冲秒数，包括尚未调度的 PCM 和已经调度但未播放的部分，最多每 500ms 一次。
- 每个 stream 按自己的缓冲状态暂停或恢复下一句，等待发生在获取本地及全局合成名额之前。
- 反馈绑定用户、会话、连接、stream 和递增序号；旧连接、乱序反馈、非法值不会影响其他流。
- 没有反馈的旧客户端直接放行；反馈过期时恢复合成，避免网络丢失造成永久等待。
- 取消语音会唤醒播放等待并释放合成任务，文字回复继续完成。

配置见 `backend/config/.env.example`，默认值：

```dotenv
TTS_MAX_CONCURRENCY=10
TTS_GLOBAL_MAX_CONCURRENCY=10
TTS_PLAYBACK_HIGH_WATER_SECONDS=8
TTS_PLAYBACK_LOW_WATER_SECONDS=4
TTS_PLAYBACK_FEEDBACK_TIMEOUT_SECONDS=3
TTS_FIRST_SEGMENT_MIN_CHARS=16
TTS_FIRST_SEGMENT_MAX_CHARS=64
TTS_FIRST_SEGMENT_WAIT_MS=350
TTS_SENTENCE_MERGE_MAX_CHARS=80
```

8 秒是下一句的暂停阈值，不是浏览器缓存硬上限：当前已开始的句子和反馈传输延迟可能让缓存超过它。
350ms 是有合适边界时的首段等待预算，不代表用户发送后 350ms 就能听到声音；LLM、排队、合成和浏览器起播仍需时间。
`TTS_STAGE_*` 目前主要用于状态展示，真正驱动模型的配置是 `fish_tts/config/fish_s2_pro_dynamic_batch.yaml`。

语音开启时 SSE 的完成仍等待文字与语音合成；缓冲节流可能延长 SSE 的持续时间，但不会暂停文字生成或其保存。
需要打断时，关闭页面“员工语音”开关或开始录音均会停止当前语音。输入的下一轮消息按现有队列顺序处理。

## 已有自动化验证的范围

- `tests/test_four_user_speech_playback.py`：两个模拟后端共享四个全局名额，四路同时进入合成，反馈等待时不占名额，四份文字完整，音频流隔离，以及取消一人后另外三人继续。
- `tests/test_tts_adaptive_segmentation.py`：首段时限、软边界、小数跨 delta、后续合并、内容顺序和取消。
- `tests/test_speech_playback_buffer.py`：高低阈值、超时、乱序/非法反馈、用户隔离与重连。
- `frontend/tests/speechPlaybackProgress.test.ts`：四个独立反馈实例、节流、暂停的 AudioContext、旧连接和清理。

这些测试使用模拟推理或时钟，不能替代真实 GPU 吞吐和四台浏览器试听。

## 真实模型验收

先确认已经取得模型使用授权、模型文件及音色已注册，且可以启动模型。不要直接把开发环境的 `TTS_ENABLED=false` 改成 true 后就视为达标。
每次测试前检查实际 GPU 分配和显存余量，不假设 Fish 与其他服务独占或共享同一卡。2026-09-06 的起测配置为 Fish 使用 GPU 1、reranker 使用 GPU 0；不要为压测擅自迁移其他模型。出现显存不足或其他服务明显变慢时停止加压。

复用直连模型的测试脚本，示例：

```bash
python scripts/benchmark_fish_tts.py \
  --concurrency 4 \
  --rounds 3 \
  --text-file loadtests/data/tts/sustained-reply.zh.txt \
  --max-new-tokens 2048 \
  --playback-buffer-seconds 1.4 \
  --max-playback-gap-seconds 0.1 \
  --min-common-playback-seconds 10 \
  --output loadtests/results/fish-four-user.json \
  --require-continuous-playback
```

可用 `--text-file path/to/input.txt` 指定 UTF-8 职场对话文本，与 `--text` 互斥。JSON 同时输出到终端与报告文件，记录文本字符数及 SHA-256；原始文本应由测试者单独保存，不使用真实用户的私密对话。

每次模型配置重启后先执行不计分的暖轮，再运行正式多轮测试，避免把模型初始化、图编译或新 batch 形状编译计入稳定吞吐。正式报告同时记录使用的模型 YAML、模型版本、GPU、并发数和起测时间。

这里的 0.1 秒和连续共同播放 10 秒是可调整的起测验收阈值。长文本直连测试使用 2048 token 上限避免单段过早触顶；网站仍按现有设置分句，不能把两者作为相同工作负载混算。脚本根据 HTTP PCM 包到达时间模拟当前前端的 1.4 秒起播/重缓冲规则。
它不包含后端排队、网络到浏览器、浏览器主线程开销和实际听感；输出的 `validation_scope` 必须一起记录，不得把直连结果当成完整网站结果。
共同播放区间必须存在；持续能力验证还应使用足够长的文本、检查共同播放时长，而不是只看一瞬间重叠。

还可执行真实 Fish HTTP + 应用合成限流 + 内存语音接收端的合成多用户测试：

```bash
python -m scripts.benchmark_fish_pipeline \
  --url http://127.0.0.1:7119/v1/audio/speech \
  --concurrency 4 --slots 4 --scenario both \
  --min-common-playback-seconds 10 \
  --require-continuous-playback \
  --output loadtests/results/fish-pipeline-four-user.json
```

`--concurrency` 是独立合成用户数，`--slots` 是该测试进程允许的合成容量。此脚本只在进程内开启 TTS，不修改 `.env`，不创建数据库会话、不调用 LLM，也不覆盖 Redis、真实 WebSocket 网络或浏览器。取消场景要求其他每路在取消时仍运行、之后继续接收 PCM 并正常完成；不满足即不能宣称取消隔离通过。
两种脚本的 PCM/哈希校验都不能证明语义朗读完整、没有静音或听感自然；仍需抽样试听或转写对照。报告中的验证范围与原始指标应一起保留。

模型直连通过后，再用四个独立账号和会话通过网站同时发送消息，至少检查：

1. 四人都收到并播放各自的完整语音，不能把“仅有文字兜底”计为通过。
2. 记录每人的用户发送到实际起播时间、最长中断、完成情况及四人共同播放时长，不只看平均值。
3. 使用较长回复以及错峰开始/结束场景，观察续句是否断开。
4. 停止一个会话的语音，确认其他三人的声音和文字持续正常；被停止的会话能开始下一轮。
5. 检查四人同时使用时 reranker、ASR 和文字回复是否出现明显退化。

尚未执行网站这一步时，不得把模型和内存模拟客户端结果称为真实多浏览器播放验收通过。
