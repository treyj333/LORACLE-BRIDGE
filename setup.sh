#!/usr/bin/env bash
#
# setup.sh — One-command installer for Project N.O.M.A.D. development environment
#
# Usage:
#   curl -fsSL <raw-url>/setup.sh | bash
#   — or —
#   ./setup.sh
#
set -eo pipefail

echo ""
echo "╔══════════════════════════════════════════════════════════════╗"
echo "║           Project N.O.M.A.D. — Dev Environment Setup       ║"
echo "╚══════════════════════════════════════════════════════════════╝"
echo ""

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" 2>/dev/null && pwd || pwd)"

# ─── Detect OS ───────────────────────────────────────────────────────────────

OS="$(uname -s)"
ARCH="$(uname -m)"

if [ "$OS" = "Darwin" ]; then
  echo "Detected: macOS ($ARCH)"
elif [ "$OS" = "Linux" ]; then
  echo "Detected: Linux ($ARCH)"
else
  echo "Unsupported OS: $OS"
  exit 1
fi

# ─── Helper ──────────────────────────────────────────────────────────────────

need_install() {
  ! command -v "$1" &>/dev/null
}

# ─── 1. Package manager ─────────────────────────────────────────────────────

if [ "$OS" = "Darwin" ]; then
  if need_install brew; then
    echo ""
    echo "Installing Homebrew..."
    /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
    # Add brew to PATH for Apple Silicon
    if [ "$ARCH" = "arm64" ] && [ -f /opt/homebrew/bin/brew ]; then
      eval "$(/opt/homebrew/bin/brew shellenv)"
    fi
  fi
  echo "OK Homebrew"
fi

# ─── 2. Node.js ─────────────────────────────────────────────────────────────

if need_install node; then
  echo ""
  echo "Installing Node.js..."
  if [ "$OS" = "Darwin" ]; then
    brew install node
  else
    curl -fsSL https://deb.nodesource.com/setup_20.x | sudo -E bash -
    sudo apt-get install -y nodejs
  fi
fi

NODE_VERSION=$(node -v | sed 's/v//' | cut -d. -f1)
if [ "$NODE_VERSION" -lt 20 ]; then
  echo "Node.js 20+ required (found v$NODE_VERSION). Please upgrade."
  exit 1
fi
echo "OK Node.js $(node -v)"

# ─── 3. MySQL ────────────────────────────────────────────────────────────────

if [ "$OS" = "Darwin" ]; then
  if need_install mysql; then
    echo ""
    echo "Installing MySQL..."
    brew install mysql
  fi
  # Start MySQL if not running
  if ! pgrep -x mysqld >/dev/null 2>&1; then
    echo "Starting MySQL..."
    brew services start mysql
    sleep 3
  fi
  echo "OK MySQL"

  # Create dev database and user if they don't exist
  echo "Configuring MySQL dev database..."
  mysql -u root -e "CREATE DATABASE IF NOT EXISTS nomad;" 2>/dev/null || \
    mysql -u root -p -e "CREATE DATABASE IF NOT EXISTS nomad;" 2>/dev/null || true
  mysql -u root -e "CREATE USER IF NOT EXISTS 'nomad_user'@'localhost' IDENTIFIED BY 'nomad_dev';" 2>/dev/null || true
  mysql -u root -e "GRANT ALL PRIVILEGES ON nomad.* TO 'nomad_user'@'localhost'; FLUSH PRIVILEGES;" 2>/dev/null || true
  echo "OK Database: nomad (user: nomad_user)"

elif [ "$OS" = "Linux" ]; then
  if need_install mysql; then
    echo ""
    echo "Installing MySQL..."
    sudo apt-get update
    sudo apt-get install -y mysql-server
    sudo systemctl start mysql
    sudo systemctl enable mysql
  fi
  if ! systemctl is-active --quiet mysql 2>/dev/null; then
    sudo systemctl start mysql
  fi
  echo "OK MySQL"
  sudo mysql -e "CREATE DATABASE IF NOT EXISTS nomad;" 2>/dev/null || true
  sudo mysql -e "CREATE USER IF NOT EXISTS 'nomad_user'@'localhost' IDENTIFIED BY 'nomad_dev';" 2>/dev/null || true
  sudo mysql -e "GRANT ALL PRIVILEGES ON nomad.* TO 'nomad_user'@'localhost'; FLUSH PRIVILEGES;" 2>/dev/null || true
  echo "OK Database: nomad (user: nomad_user)"
fi

# ─── 4. Redis ────────────────────────────────────────────────────────────────

if [ "$OS" = "Darwin" ]; then
  if need_install redis-server && need_install redis-cli; then
    echo ""
    echo "Installing Redis..."
    brew install redis
  fi
  if ! pgrep -x redis-server >/dev/null 2>&1; then
    echo "Starting Redis..."
    brew services start redis
    sleep 1
  fi
  echo "OK Redis"

elif [ "$OS" = "Linux" ]; then
  if need_install redis-server; then
    echo ""
    echo "Installing Redis..."
    sudo apt-get install -y redis-server
    sudo systemctl start redis-server
    sudo systemctl enable redis-server
  fi
  if ! systemctl is-active --quiet redis-server 2>/dev/null; then
    sudo systemctl start redis-server
  fi
  echo "OK Redis"
fi

# ─── 5. Python 3 (for Meshtastic bridge) ────────────────────────────────────

if need_install python3; then
  echo ""
  echo "Installing Python 3..."
  if [ "$OS" = "Darwin" ]; then
    brew install python3
  else
    sudo apt-get install -y python3 python3-venv python3-pip
  fi
fi
echo "OK Python $(python3 --version | cut -d' ' -f2)"

# ─── 6. Install npm dependencies ────────────────────────────────────────────

echo ""
echo "Installing npm dependencies..."
cd "$PROJECT_ROOT"
npm install --ignore-scripts 2>/dev/null
echo "OK npm packages"

# ─── 7. Run database migrations ─────────────────────────────────────────────

echo ""
echo "Running database migrations..."
export NODE_ENV=development
export DB_HOST=127.0.0.1
export DB_PORT=3306
export DB_DATABASE=nomad
export DB_USER=nomad_user
export DB_PASSWORD=nomad_dev
export REDIS_HOST=127.0.0.1
export REDIS_PORT=6379
export APP_KEY="dev-app-key-at-least-16chars"
export URL="http://localhost:8080"

cd "$PROJECT_ROOT/admin"
node ace migration:run --force 2>/dev/null || node ace migration:run || echo "Migrations may need manual review"
cd "$PROJECT_ROOT"

# ─── Done ────────────────────────────────────────────────────────────────────

echo ""
echo "╔══════════════════════════════════════════════════════════════╗"
echo "║                    Setup Complete!                          ║"
echo "╠══════════════════════════════════════════════════════════════╣"
echo "║                                                            ║"
echo "║  To start N.O.M.A.D.:                                     ║"
echo "║    ./dev.sh                                                ║"
echo "║                                                            ║"
echo "║  To start with Meshtastic bridge:                          ║"
echo "║    ./dev.sh --with-mesh                                    ║"
echo "║                                                            ║"
echo "║  Open in browser:                                          ║"
echo "║    http://localhost:8080                                   ║"
echo "║                                                            ║"
echo "╚══════════════════════════════════════════════════════════════╝"
echo ""
