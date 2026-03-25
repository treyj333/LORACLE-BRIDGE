#!/usr/bin/env bash
#
# start.sh — One-command production launcher for Project N.O.M.A.D.
#
# Usage:
#   ./start.sh              Start all core services (admin, MySQL, Redis, Dozzle, etc.)
#   ./start.sh --with-mesh  Also start the Meshtastic bridge service
#   ./start.sh --stop       Stop all running services
#   ./start.sh --logs       Tail logs from all services
#
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "$0")" && pwd)"
COMPOSE_FILE="$PROJECT_ROOT/install/management_compose.yaml"

# ─── Parse arguments ─────────────────────────────────────────────────────────

ACTION="start"
WITH_MESH=false

for arg in "$@"; do
  case "$arg" in
    --with-mesh) WITH_MESH=true ;;
    --stop) ACTION="stop" ;;
    --logs) ACTION="logs" ;;
    --help|-h)
      echo "Usage: ./start.sh [OPTIONS]"
      echo ""
      echo "Options:"
      echo "  --with-mesh   Also start the Meshtastic bridge service"
      echo "  --stop        Stop all running services"
      echo "  --logs        Tail logs from all services"
      echo "  --help        Show this help message"
      exit 0
      ;;
    *) echo "Unknown option: $arg"; echo "Run ./start.sh --help for usage."; exit 1 ;;
  esac
done

# ─── Check prerequisites ────────────────────────────────────────────────────

if ! command -v docker &>/dev/null; then
  echo "❌ Docker not found. Install Docker first."
  exit 1
fi

if [ ! -f "$COMPOSE_FILE" ]; then
  echo "❌ Compose file not found at: $COMPOSE_FILE"
  exit 1
fi

# Check if 'replaceme' values still exist in the compose file
if grep -q "replaceme" "$COMPOSE_FILE"; then
  echo "⚠️  Warning: Found 'replaceme' values in $COMPOSE_FILE"
  echo "   Please edit the file and set APP_KEY, URL, and database passwords before starting."
  echo ""
  read -p "Continue anyway? (y/N) " -n 1 -r
  echo
  if [[ ! $REPLY =~ ^[Yy]$ ]]; then
    exit 1
  fi
fi

# ─── Execute action ──────────────────────────────────────────────────────────

MESH_PROFILE=""
if [ "$WITH_MESH" = true ]; then
  MESH_PROFILE="--profile meshtastic"
fi

case "$ACTION" in
  start)
    echo "🚀 Starting Project N.O.M.A.D...."
    docker compose -f "$COMPOSE_FILE" $MESH_PROFILE up -d
    echo ""
    echo "✅ All services started!"
    echo ""
    echo "   Admin UI:  Check your URL setting in the compose file (default: http://localhost:8080)"
    echo "   Logs:      Run ./start.sh --logs"
    echo "   Stop:      Run ./start.sh --stop"
    if [ "$WITH_MESH" = true ]; then
      echo "   Meshtastic: Bridge service is running"
    fi
    ;;
  stop)
    echo "🛑 Stopping Project N.O.M.A.D...."
    docker compose -f "$COMPOSE_FILE" --profile meshtastic down
    echo "✅ All services stopped."
    ;;
  logs)
    docker compose -f "$COMPOSE_FILE" $MESH_PROFILE logs -f
    ;;
esac
