#!/usr/bin/env bash
set -euo pipefail

umask 077

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"

if [[ -n "${SPIDER_PYTHON:-}" ]]; then
  python_candidates=("${SPIDER_PYTHON}")
else
  python_candidates=(
    "${SCRIPT_DIR}/.venv/bin/python"
    "${SCRIPT_DIR}/../.venv/bin/python"
  )
  # Conda can live below different prefixes on otherwise identical servers.
  # Discover its base before falling back to the ambient python3.
  if command -v conda >/dev/null 2>&1; then
    conda_command="$(command -v conda)"
    conda_base="$(cd -- "$(dirname -- "${conda_command}")/.." && pwd)"
    python_candidates+=("${conda_base}/envs/hr_agent/bin/python")
  fi
  python_candidates+=(
    "/home/uay4sgh/miniconda3/envs/hr_agent/bin/python"
    "/home/uay4sgh/miniforge3/envs/hr_agent/bin/python"
  )
  if command -v python3 >/dev/null 2>&1; then
    python_candidates+=("$(command -v python3)")
  fi
fi

PYTHON_BIN=""
for candidate in "${python_candidates[@]}"; do
  if [[ -x "${candidate}" ]]; then
    PYTHON_BIN="${candidate}"
    break
  fi
done

if [[ -z "${PYTHON_BIN}" ]]; then
  echo "未找到可用的 Python。请创建 spider/.venv，或设置 SPIDER_PYTHON=/绝对路径/python。" >&2
  exit 1
fi

export PYTHONIOENCODING=utf-8
export PYTHONUTF8=1
export PYTHONUNBUFFERED=1

exec "${PYTHON_BIN}" -u "${SCRIPT_DIR}/run_pipeline.py" "$@"
