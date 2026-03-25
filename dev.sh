#!/usr/bin/env bash
#
# dev.sh — One-command dev launcher for Project N.O.M.A.D.
#
# Usage:
#   ./dev.sh              Start N.O.M.A.D. dev server (MySQL, Redis, queue worker, web server)
#   ./dev.sh --with-mesh  Also start the Meshtastic Python bridge
#   ./dev.sh --setup      Run full setup first (install prerequisites), then start
#
set -eo pipefail

PROJECT_ROOT="$(cd "$(dirname "$0")" && pwd)"
cd "$PROJECT_ROOT"

WITH_MESH=false
RUN_SETUP=false
for arg in "$@"; do
  case "$arg" in
    --with-mesh) WITH_MESH=true ;;
    --setup) RUN_SETUP=true ;;
    *) echo "Unknown option: $arg"; echo "Usage: ./dev.sh [--with-mesh] [--setup]"; exit 1 ;;
  esac
done

# Track background PIDs for cleanup
declare -a PIDS
STARTED_MYSQL=false
STARTED_REDIS=false

cleanup() {
  echo ""
  echo "Shutting down..."
  if [ ${#PIDS[@]} -gt 0 ]; then
    for pid in "${PIDS[@]}"; do
      if kill -0 "$pid" 2>/dev/null; then
        kill "$pid" 2>/dev/null || true
      fi
    done
  fi
  # Stop Docker containers only if we started them
  if [ "$STARTED_MYSQL" = true ] && command -v docker &>/dev/null; then
    echo "Stopping dev MySQL container..."
    docker stop nomad_dev_mysql >/dev/null 2>&1 || true
  fi
  if [ "$STARTED_REDIS" = true ] && command -v docker &>/dev/null; then
    echo "Stopping dev Redis container..."
    docker stop nomad_dev_redis >/dev/null 2>&1 || true
  fi
  echo "All processes stopped."
  exit 0
}

trap cleanup EXIT INT TERM

# ─── Run setup if requested ──────────────────────────────────────────────────

if [ "$RUN_SETUP" = true ]; then
  bash "$PROJECT_ROOT/setup.sh"
fi

# ─── 1. Check prerequisites ─────────────────────────────────────────────────

echo "Checking prerequisites..."

if ! command -v node &>/dev/null; then
  echo "Node.js not found. Run: ./setup.sh  (or ./dev.sh --setup)"
  exit 1
fi

HAS_DOCKER=false
if command -v docker &>/dev/null; then
  HAS_DOCKER=true
fi

# ─── 2. Start MySQL ─────────────────────────────────────────────────────────

MYSQL_RUNNING=false

# Check native MySQL first
if pgrep -x mysqld >/dev/null 2>&1; then
  echo "OK MySQL (native)"
  MYSQL_RUNNING=true
elif command -v brew &>/dev/null && brew services list 2>/dev/null | grep -q "mysql.*started"; then
  echo "OK MySQL (brew service)"
  MYSQL_RUNNING=true
elif systemctl is-active --quiet mysql 2>/dev/null; then
  echo "OK MySQL (systemd)"
  MYSQL_RUNNING=true
fi

# Try Docker container if native isn't running
if [ "$MYSQL_RUNNING" = false ] && [ "$HAS_DOCKER" = true ]; then
  if docker ps --format '{{.Names}}' 2>/dev/null | grep -q "nomad_dev_mysql"; then
    echo "OK MySQL (Docker container)"
    MYSQL_RUNNING=true
  else
    echo "Starting MySQL via Docker..."
    docker rm -f nomad_dev_mysql 2>/dev/null || true
    docker run -d \
      --name nomad_dev_mysql \
      -e MYSQL_ROOT_PASSWORD=nomad_dev \
      -e MYSQL_DATABASE=nomad \
      -e MYSQL_USER=nomad_user \
      -e MYSQL_PASSWORD=nomad_dev \
      -p 3306:3306 \
      mysql:8.0 >/dev/null
    echo "Waiting for MySQL..."
    for i in $(seq 1 30); do
      if docker exec nomad_dev_mysql mysqladmin ping -h localhost --silent 2>/dev/null; then
        break
      fi
      sleep 1
    done
    echo "OK MySQL (Docker container)"
    MYSQL_RUNNING=true
    STARTED_MYSQL=true
  fi
fi

if [ "$MYSQL_RUNNING" = false ]; then
  echo "MySQL not found. Run: ./setup.sh  (or install MySQL manually)"
  exit 1
fi

# ─── 3. Start Redis ─────────────────────────────────────────────────────────

REDIS_RUNNING=false

# Check native Redis first
if pgrep -x redis-server >/dev/null 2>&1; then
  echo "OK Redis (native)"
  REDIS_RUNNING=true
elif command -v brew &>/dev/null && brew services list 2>/dev/null | grep -q "redis.*started"; then
  echo "OK Redis (brew service)"
  REDIS_RUNNING=true
elif systemctl is-active --quiet redis-server 2>/dev/null; then
  echo "OK Redis (systemd)"
  REDIS_RUNNING=true
fi

# Try Docker container if native isn't running
if [ "$REDIS_RUNNING" = false ] && [ "$HAS_DOCKER" = true ]; then
  if docker ps --format '{{.Names}}' 2>/dev/null | grep -q "nomad_dev_redis"; then
    echo "OK Redis (Docker container)"
    REDIS_RUNNING=true
  else
    echo "Starting Redis via Docker..."
    docker rm -f nomad_dev_redis 2>/dev/null || true
    docker run -d \
      --name nomad_dev_redis \
      -p 6379:6379 \
      redis:7-alpine >/dev/null
    echo "OK Redis (Docker container)"
    REDIS_RUNNING=true
    STARTED_REDIS=true
  fi
fi

if [ "$REDIS_RUNNING" = false ]; then
  echo "Redis not found. Run: ./setup.sh  (or install Redis manually)"
  exit 1
fi

# ─── 4. Install dependencies if needed ──────────────────────────────────────

if [ ! -d "node_modules" ]; then
  echo "Installing npm dependencies..."
  npm install --ignore-scripts
fi

# ─── 5. Set environment variables for dev ────────────────────────────────────

export NODE_ENV=development
export DB_HOST=127.0.0.1
export DB_PORT=3306
export DB_DATABASE=nomad
export DB_USER=nomad_user
export DB_PASSWORD=nomad_dev
export REDIS_HOST=127.0.0.1
export REDIS_PORT=6379
export HOST=0.0.0.0
export PORT=8080
export APP_KEY="${APP_KEY:-dev-app-key-at-least-16chars}"
export URL="${URL:-http://localhost:8080}"

# ─── 6. Run migrations ──────────────────────────────────────────────────────

echo "Running database migrations..."
cd admin
node ace migration:run --force 2>/dev/null || node ace migration:run || echo "Warning: migrations may need review"
cd "$PROJECT_ROOT"

# ─── 7. Start queue worker in background ────────────────────────────────────

echo "Starting queue worker..."
cd admin
node ace queue:work --all &
PIDS+=($!)
cd "$PROJECT_ROOT"

# ─── 8. Optionally start Meshtastic bridge ──────────────────────────────────

if [ "$WITH_MESH" = true ]; then
  if [ -d "meshtastic-bridge" ]; then
    echo "Starting Meshtastic bridge..."
    cd meshtastic-bridge
    if [ ! -d "venv" ]; then
      echo "   Creating Python virtual environment..."
      python3 -m venv venv
      venv/bin/pip install -r requirements.txt -q
    fi
    NOMAD_API_URL="http://localhost:8080" venv/bin/python bridge.py &
    PIDS+=($!)
    cd "$PROJECT_ROOT"
    echo "OK Meshtastic bridge started"
  else
    echo "Warning: meshtastic-bridge/ directory not found, skipping"
  fi
fi

# ─── 9. Start AdonisJS dev server (foreground) ──────────────────────────────

echo ""
echo "=========================================="
echo "  N.O.M.A.D. is starting..."
echo "  http://localhost:${PORT}"
if [ "$WITH_MESH" = true ]; then
  echo "  Meshtastic bridge: active"
fi
echo "  Press Ctrl+C to stop everything"
echo "=========================================="
echo ""

cd admin
node ace serve --watch
