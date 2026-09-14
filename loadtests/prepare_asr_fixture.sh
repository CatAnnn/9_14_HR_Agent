#!/bin/sh
set -eu

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
OUTPUT=${1:-"${SCRIPT_DIR}/data/audio/asr-sample.pcm"}
OUTPUT_DIR=$(dirname -- "${OUTPUT}")
OUTPUT_NAME=$(basename -- "${OUTPUT}")
IMAGE=${QWEN3_ASR_IMAGE:-qwenllm/qwen3-asr:latest@sha256:fb75b775f089e06e5a1aaebffd421e37505cc630d50c86d889d95ffa45a7e16a}
SOURCE=/usr/local/lib/python3.10/dist-packages/gradio/media_assets/audio/recording1.wav

mkdir -p "${OUTPUT_DIR}"
OUTPUT_DIR=$(CDPATH= cd -- "${OUTPUT_DIR}" && pwd)

docker run --rm \
  --volume "${OUTPUT_DIR}:/out" \
  --user "$(id -u):$(id -g)" \
  --entrypoint ffmpeg \
  "${IMAGE}" \
  -hide_banner -loglevel error -y \
  -stream_loop 3 -i "${SOURCE}" -t 10 \
  -ar 16000 -ac 1 -f s16le "/out/${OUTPUT_NAME}"

chmod 0644 "${OUTPUT_DIR}/${OUTPUT_NAME}"
BYTES=$(wc -c < "${OUTPUT_DIR}/${OUTPUT_NAME}")
if [ "$((BYTES % 2))" -ne 0 ] || [ "${BYTES}" -lt 300000 ]; then
  echo "Invalid PCM fixture: ${BYTES} bytes" >&2
  exit 1
fi

echo "Prepared ${OUTPUT_DIR}/${OUTPUT_NAME} (${BYTES} bytes, 16 kHz mono PCM16)."
