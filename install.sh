#!/usr/bin/env sh
# agentchattr global install — sets up the venv and puts an `agentchattr`
# command on PATH so per-project swarms can be launched from any directory:
#
#     cd ~/projects/myapp
#     agentchattr up claude codex agy agy
#
# Idempotent and sudo-free: re-run after `git pull` to refresh dependencies
# and the launcher shim.

set -e

REPO="$(cd "$(dirname "$0")" && pwd)"
cd "$REPO"

PYTHON_BIN=""
if command -v python3 >/dev/null 2>&1; then
    PYTHON_BIN="python3"
elif command -v python >/dev/null 2>&1; then
    PYTHON_BIN="python"
else
    echo "Python 3 is required but was not found on PATH."
    exit 1
fi

if ! "$PYTHON_BIN" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)'; then
    echo "Error: Python 3.11+ is required (found $("$PYTHON_BIN" --version 2>&1))."
    exit 1
fi

if [ -d ".venv" ] && [ ! -x ".venv/bin/python" ]; then
    echo "Recreating .venv for this platform..."
    rm -rf .venv
fi

if [ ! -x ".venv/bin/python" ]; then
    echo "Creating virtual environment..."
    "$PYTHON_BIN" -m venv .venv || {
        echo "Error: failed to create .venv with $PYTHON_BIN."
        exit 1
    }
fi

echo "Installing/refreshing dependencies..."
.venv/bin/python -m pip install -q -r requirements.txt || {
    echo "Error: failed to install Python dependencies."
    exit 1
}

if ! command -v tmux >/dev/null 2>&1; then
    echo ""
    echo "Warning: tmux is not installed — agents can't run without it."
    echo "Install it with: sudo apt install tmux"
fi

BIN_DIR="$HOME/.local/bin"
mkdir -p "$BIN_DIR" "$HOME/.agentchattr/instances"

SHIM="$BIN_DIR/agentchattr"
cat > "$SHIM" <<EOF
#!/bin/sh
exec "$REPO/.venv/bin/python" "$REPO/agentchattr_cli.py" "\$@"
EOF
chmod +x "$SHIM"
echo "Installed launcher: $SHIM"

case ":$PATH:" in
    *":$BIN_DIR:"*) ;;
    *)
        echo ""
        echo "Note: $BIN_DIR is not on your PATH yet."
        echo "On Ubuntu, ~/.profile adds it automatically once it exists —"
        echo "log out/in, or run:  export PATH=\"\$HOME/.local/bin:\$PATH\""
        ;;
esac

echo ""
echo "Done. From any project directory:"
echo "  agentchattr up claude codex agy agy   # start a swarm here"
echo "  agentchattr status                    # what's running"
echo "  agentchattr attach <name>             # watch an agent"
echo "  agentchattr down                      # stop this project's swarm"
