#!/usr/bin/env bash
set -e

echo "=== [1/3] Installing Python packages ==="
pip install -r requirements.txt

echo "=== [2/3] Setting up Node.js 20 LTS runtime for WhatsApp Baileys ==="
NODE_VER="v20.18.1"
NODE_DIR=".node_runtime"
if [ ! -x "$NODE_DIR/bin/node" ]; then
    rm -rf "$NODE_DIR"
    mkdir -p "$NODE_DIR"
    curl -fsSL "https://nodejs.org/dist/${NODE_VER}/node-${NODE_VER}-linux-x64.tar.xz" | tar -xJ -C "$NODE_DIR" --strip-components=1
fi
export PATH="$PWD/$NODE_DIR/bin:$PATH"
node -v
npm -v

echo "=== [3/3] Installing WhatsApp Baileys bridge dependencies ==="
cd wa_bridge
npm install --no-audit --no-fund
cd ..
echo "=== Build complete! ==="
