#!/usr/bin/env bash
# One-shot setup for sgai-demo: pulls Ollama models and registers sample ad templates.
# Run once after `cp .env.example .env` and before `docker compose up --build`.
#
# Each step is safe to re-run — models and templates already present are skipped.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "======================================="
echo " sgai-demo setup"
echo "======================================="
echo ""
echo "── 1/2  Ollama fusion model ────────────"
"$SCRIPT_DIR/stream-lens/pull-models.sh"

echo ""
echo "── 2/2  Ad templates ───────────────────"
"$SCRIPT_DIR/real-time-ad-gen/setup-templates.sh" --compose-file "$SCRIPT_DIR/docker-compose.yml"

echo ""
echo "======================================="
echo " Setup complete."
echo " Next: docker compose up --build"
echo "       open http://localhost:8082"
echo "======================================="
