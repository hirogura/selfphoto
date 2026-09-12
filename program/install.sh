#!/usr/bin/env bash
# selfphoto インストール（program/ 配下から実行する場合のラッパー）
#
# 実体はリポジトリ直下の install.sh。program/ に cd しても
#   sudo ./install.sh
# でインストールできるようにしている。
set -euo pipefail

PROGRAM_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(dirname "$PROGRAM_DIR")"

exec bash "$REPO_ROOT/install.sh" "$@"
