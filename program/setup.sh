#!/usr/bin/env bash
# selfphoto セットアップ: Pillow をシステムにインストールする
set -euo pipefail

if python3 -c "import PIL" 2>/dev/null; then
  echo "Pillow already installed"
else
  echo "Installing python3-pil (apt)..."
  apt-get update -qq && apt-get install -y -qq python3-pil
fi

echo "OK: python3 -c 'import PIL'"
python3 -c "import PIL; print('Pillow', PIL.__version__)"
