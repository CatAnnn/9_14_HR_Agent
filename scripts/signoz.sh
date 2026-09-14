#!/usr/bin/env bash
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SIGNOZ_DIR="${PROJECT_ROOT}/observability/signoz"
POURS_DIR="${SIGNOZ_DIR}/pours"
COMPOSE_FILE="${POURS_DIR}/deployment/compose.yaml"
FOUNDRYCTL="${FOUNDRYCTL:-foundryctl}"

usage() {
  echo "Usage: $0 {deploy|wake|status|logs|down}"
}

require_foundry() {
  if ! command -v "${FOUNDRYCTL}" >/dev/null 2>&1; then
    echo "foundryctl is required. Install it with: curl -fsSL https://signoz.io/foundry.sh | bash" >&2
    exit 1
  fi
}

case "${1:-}" in
  deploy)
    require_foundry
    cd "${SIGNOZ_DIR}"
    "${FOUNDRYCTL}" cast -f casting.yaml -p pours
    ;;
  wake)
    docker compose -f "${COMPOSE_FILE}" up -d
    ;;
  status)
    docker compose -f "${COMPOSE_FILE}" ps
    ;;
  logs)
    docker compose -f "${COMPOSE_FILE}" logs -f signoz-signoz-0 ingester
    ;;
  down)
    docker compose -f "${COMPOSE_FILE}" down
    ;;
  *)
    usage
    exit 1
    ;;
esac
