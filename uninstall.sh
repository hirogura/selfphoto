#!/usr/bin/env bash
# selfphoto アンインストーラ
#
# systemd ユニットの停止・無効化・削除と、/opt/selfphoto からのプログラム削除を行う。
# 写真データ・サムネイル・DB（/opt/lxd-data/selfphoto-data 配下）は既定では削除しない。
# データごと完全に削除する場合は --purge を付けて実行する。
#
# 使い方:
#   sudo ./uninstall.sh           # プログラムと systemd 登録を削除（データは保持）
#   sudo ./uninstall.sh --purge   # 写真・サムネイル・DB を含め完全削除
set -euo pipefail

SELFPHPHOTO_HOME="${SELFPHPHOTO_HOME:-/opt/selfphoto}"
# データ領域（install.sh と同じ既定値。写真・サムネイル・DB はこちら）
SELFPHPHOTO_DATA_ROOT="${SELFPHPHOTO_DATA_ROOT:-/opt/lxd-data}"
SELFPHPHOTO_DATA_DIR="${SELFPHPHOTO_DATA_DIR:-${SELFPHPHOTO_DATA_ROOT}/selfphoto-data}"
SELFPHPHOTO_PHOTO_DIR="${SELFPHPHOTO_PHOTO_DIR:-${SELFPHPHOTO_DATA_DIR}/photo}"
PURGE=0
for arg in "$@"; do
  case "$arg" in
    --purge) PURGE=1 ;;
    -h|--help)
      sed -n '2,12p' "$0" | sed 's/^# \{0,1\}//'
      exit 0
      ;;
    *)
      echo "不明なオプション: $arg（--purge のみ対応）" >&2
      exit 1
      ;;
  esac
done

if [[ "${EUID:-$(id -u)}" -ne 0 ]]; then
  echo "エラー: root 権限で実行してください（sudo ./uninstall.sh）" >&2
  exit 1
fi

echo "=== selfphoto アンインストール: ${SELFPHPHOTO_HOME} ==="

# ===== systemd ユニット停止・無効化・削除 =====
if command -v systemctl >/dev/null 2>&1 && [[ -d /etc/systemd/system ]]; then
  systemctl disable --now selfphoto-server.service 2>/dev/null || true
  systemctl disable --now selfphoto-scan.timer selfphoto-thumbs.timer 2>/dev/null || true
  rm -f /etc/systemd/system/selfphoto-*.service /etc/systemd/system/selfphoto-*.timer
  systemctl daemon-reload
  systemctl reset-failed 'selfphoto-*' 2>/dev/null || true
  echo "systemd ユニットを削除しました"
fi

# ===== プログラム・ドキュメント削除（データは保持） =====
rm -rf "${SELFPHPHOTO_HOME}/program"
rm -f  "${SELFPHPHOTO_HOME}/README.md" "${SELFPHPHOTO_HOME}/LICENSE" "${SELFPHPHOTO_HOME}/uninstall.sh"
echo "プログラムを削除しました（${SELFPHPHOTO_HOME}/program）"

# ===== データ削除（--purge 時のみ） =====
if [[ "$PURGE" -eq 1 ]]; then
  rm -rf "${SELFPHPHOTO_HOME}"
  rm -rf "${SELFPHPHOTO_DATA_DIR}"
  echo "データを含め削除しました (--purge):"
  echo "  ${SELFPHPHOTO_HOME}"
  echo "  ${SELFPHPHOTO_DATA_DIR}"
else
  if [[ -d "${SELFPHPHOTO_DATA_DIR}" ]]; then
    echo "写真データは保持されています:"
    echo "  ${SELFPHPHOTO_PHOTO_DIR}"
    echo "  ${SELFPHPHOTO_DATA_DIR}/thumbnail"
    echo "データごと削除する場合: sudo ./uninstall.sh --purge"
  fi
  if [[ -d "${SELFPHPHOTO_HOME}" ]]; then
    echo
    echo "注意: 旧配置のデータが残っている場合は ${SELFPHPHOTO_HOME} 配下を確認してください"
  fi
fi

echo
echo "=== アンインストール完了 ==="
