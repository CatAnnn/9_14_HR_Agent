FROM vllm/vllm-omni:v0.24.0@sha256:ffb9a2a8e78a848e80ce40674ab7f36d7653358b30e616629f3ba9a235873b79

ARG HTTP_PROXY
ARG HTTPS_PROXY
ARG NO_PROXY
ARG FISH_SPEECH_VERSION=0.1.0
ARG FISH_SPEECH_WHEEL_SHA256=1ea6123c7b9a441bc48f296439ae0e987621b780f9314ff3eb5ad072ebe9e7ac

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

RUN set -eux; \
    wheel_dir=/tmp/fish-speech-wheel; \
    wheel_path="$wheel_dir/fish_speech-${FISH_SPEECH_VERSION}-py3-none-any.whl"; \
    install -d "$wheel_dir"; \
    python3 -m pip install \
      "argbind==0.3.9" \
      "ffmpy==0.6.1" \
      "flatten-dict==0.4.2" \
      "hydra-core==1.3.2" \
      "importlib-resources==6.5.2" \
      "julius==0.2.7" \
      "librosa==0.11.0" \
      "randomname==0.2.1" \
      "tensorboard==2.20.0"; \
    python3 -m pip install --no-deps \
      "descript-audio-codec==1.0.0" \
      "descript-audiotools==0.7.2"; \
    python3 -m pip download \
      --no-deps \
      --only-binary=:all: \
      --dest "$wheel_dir" \
      "fish-speech==${FISH_SPEECH_VERSION}"; \
    printf '%s  %s\n' "$FISH_SPEECH_WHEEL_SHA256" "$wheel_path" | sha256sum -c -; \
    python3 -m pip install --no-deps "$wheel_path"; \
    python3 -c "import vllm_omni; from fish_speech.models.dac import modded_dac, rvq"; \
    rm -rf "$wheel_dir"

COPY fish_tts/patches/vllm_omni_0_24_fish_cudagraph_warmup.patch /tmp/
COPY fish_tts/patches/vllm_omni_0_24_fish_interleaved_kv.patch /tmp/
COPY fish_tts/patches/vllm_omni_0_24_fish_default_voice.patch /tmp/
COPY fish_tts/patches/vllm_omni_0_24_fish_dynamic_batch.patch /tmp/
COPY fish_tts/patches/vllm_omni_0_24_fish_fast_ar_compile.patch /tmp/
COPY fish_tts/patches/vllm_omni_0_24_fish_sampling.patch /tmp/

RUN set -eux; \
    cudagraph_target=/usr/local/lib/python3.12/dist-packages/vllm_omni/worker/gpu_model_runner.py; \
    fish_attn_target=/usr/local/lib/python3.12/dist-packages/vllm_omni/attention/fish_kvcache_attn.py; \
    fish_backend_target=/usr/local/lib/python3.12/dist-packages/vllm_omni/attention/fish_kvcache_backend.py; \
    fish_triton_target=/usr/local/lib/python3.12/dist-packages/vllm_omni/attention/fish_kvcache_triton.py; \
    voice_target=/usr/local/lib/python3.12/dist-packages/vllm_omni/entrypoints/openai/tts_adapters/fish_speech.py; \
    slow_ar_target=/usr/local/lib/python3.12/dist-packages/vllm_omni/model_executor/models/fish_speech/fish_speech_slow_ar.py; \
    fast_ar_target=/usr/local/lib/python3.12/dist-packages/vllm_omni/model_executor/models/fish_speech/fish_speech_fast_ar.py; \
    dac_target=/usr/local/lib/python3.12/dist-packages/vllm_omni/model_executor/models/fish_speech/fish_speech_dac_decoder.py; \
    printf '%s  %s\n' \
      "91a1410d6780c385635f328a73a1999b022266b27fb9239212cb32f67d77a19a" \
      "$cudagraph_target" | sha256sum -c -; \
    printf '%s  %s\n' \
      "1b84f3efc36aef48b4b0380ff7be2c53b8e9890e54ffc52cee6b4a57f22056ab" \
      "$fish_attn_target" | sha256sum -c -; \
    printf '%s  %s\n' \
      "14da390c8a51eef38a8ec419127f074bdb272d617a6a8001fd08172cde6f8bf9" \
      "$fish_backend_target" | sha256sum -c -; \
    printf '%s  %s\n' \
      "c6477d4de799c5c48dc4deb713cb1eca85c073fc3e46289b6f79935e08d5a340" \
      "$fish_triton_target" | sha256sum -c -; \
    printf '%s  %s\n' \
      "3e290faf4766fc3cae77bbdfcd0f1de5bbde4898795db1fa798471f5d352e84b" \
      "$voice_target" | sha256sum -c -; \
    patch --batch --forward --fuzz=0 \
      -d /usr/local/lib/python3.12/dist-packages \
      -p1 \
      < /tmp/vllm_omni_0_24_fish_cudagraph_warmup.patch; \
    patch --batch --forward --fuzz=0 \
      -d /usr/local/lib/python3.12/dist-packages \
      -p1 \
      < /tmp/vllm_omni_0_24_fish_interleaved_kv.patch; \
    patch --batch --forward --fuzz=0 \
      -d /usr/local/lib/python3.12/dist-packages \
      -p1 \
      < /tmp/vllm_omni_0_24_fish_default_voice.patch; \
    printf '%s  %s\n' \
      "21ac086ef99c15dc998ed875d5c3be2e744007622de4fcea151480812e1441d1" \
      "$cudagraph_target" | sha256sum -c -; \
    printf '%s  %s\n' \
      "3c03cfa3db4b789fd0a642bd3be77977e43c1542c65670585c9ed87e8a873e5c" \
      "$slow_ar_target" | sha256sum -c -; \
    printf '%s  %s\n' \
      "bdf15958749ca4621f109b64a2aca53da22fe772e559b6c60202ae2e10ff4155" \
      "$fast_ar_target" | sha256sum -c -; \
    printf '%s  %s\n' \
      "5c19fe26ea22e530f02611ced08df37767d89ef9a6c4e821b84f97444453ba43" \
      "$dac_target" | sha256sum -c -; \
    patch --batch --forward --fuzz=0 \
      -d /usr/local/lib/python3.12/dist-packages \
      -p1 \
      < /tmp/vllm_omni_0_24_fish_dynamic_batch.patch; \
    printf '%s  %s\n' \
      "5f122730c2c02904cc44eb8b9cedb240b2a9f2f5ad505b87593dfe4f2678f2d7" \
      "$fast_ar_target" | sha256sum -c -; \
    printf '%s  %s\n' \
      "3718a924b4c5ae9d467469cf5787000ba1b0acf71ed8051a7d96d8f61c333fe5" \
      "$slow_ar_target" | sha256sum -c -; \
    patch --batch --forward --fuzz=0 \
      -d /usr/local/lib/python3.12/dist-packages \
      -p1 \
      < /tmp/vllm_omni_0_24_fish_fast_ar_compile.patch; \
    printf '%s  %s\n' \
      "f15c96b4273ece1328777f213f3ac4520bf084195f22e01d48b11e5b9f4e0f9d" \
      "$fast_ar_target" | sha256sum -c -; \
    patch --batch --forward --fuzz=0 \
      -d /usr/local/lib/python3.12/dist-packages \
      -p1 \
      < /tmp/vllm_omni_0_24_fish_sampling.patch; \
    python3 -m py_compile \
      "$cudagraph_target" \
      "$fish_attn_target" \
      "$fish_backend_target" \
      "$fish_triton_target" \
      "$voice_target" \
      "$slow_ar_target" \
      "$fast_ar_target" \
      "$dac_target"; \
    rm \
      /tmp/vllm_omni_0_24_fish_cudagraph_warmup.patch \
      /tmp/vllm_omni_0_24_fish_interleaved_kv.patch \
      /tmp/vllm_omni_0_24_fish_default_voice.patch \
      /tmp/vllm_omni_0_24_fish_dynamic_batch.patch \
      /tmp/vllm_omni_0_24_fish_fast_ar_compile.patch \
      /tmp/vllm_omni_0_24_fish_sampling.patch

RUN python3 -c "import importlib.metadata as m, numpy, pydantic, torch; assert m.version('vllm') == '0.24.0'; assert m.version('vllm-omni') == '0.24.0'; assert numpy.__version__ == '2.2.6'; assert pydantic.__version__ == '2.13.4'; assert torch.__version__ == '2.11.0+cu130'"
