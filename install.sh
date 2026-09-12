#!/usr/bin/env bash
# selfphoto インストーラ
#
# 任意の環境に /opt/selfphoto を作成し、プログラム一式を配置して
# systemd ユニットを登録・起動する。
# 写真・サムネイル・DB はプログラム領域と分離し、/opt/lxd-data 側に置く。
#
# 使い方:
#   sudo ./install.sh                                   # 標準の /opt/selfphoto にインストール
#   sudo SELFPHPHOTO_HOME=/srv/selfphoto ./install.sh   # 配置先を変えたい場合
#
# アップデート: 同じコマンドを再実行（上書きインストール、写真・DB は保持）
# アンインストール: sudo ./uninstall.sh
set -euo pipefail

# ===== 設定（環境変数で上書き可） =====
SELFPHPHOTO_HOME="${SELFPHPHOTO_HOME:-/opt/selfphoto}"
# 写真・サムネイル・DB などデータの置き場所（プログラム領域の外に出す）
# 写真の本体は DATA_DIR/photo 配下に集約。photo/ だけコピーすればバックアップになる。
SELFPHPHOTO_DATA_ROOT="${SELFPHPHOTO_DATA_ROOT:-/opt/lxd-data}"
SELFPHPHOTO_DATA_DIR="${SELFPHPHOTO_DATA_DIR:-${SELFPHPHOTO_DATA_ROOT}/selfphoto-data}"
SELFPHPHOTO_PHOTO_DIR="${SELFPHPHOTO_PHOTO_DIR:-${SELFPHPHOTO_DATA_DIR}/photo}"
SELFPHPHOTO_THUMB_DIR="${SELFPHPHOTO_THUMB_DIR:-${SELFPHPHOTO_DATA_DIR}/thumbnail}"
SELFPHPHOTO_EDIT_DIR="${SELFPHPHOTO_EDIT_DIR:-${SELFPHPHOTO_DATA_DIR}/edit-photo}"
SELFPHPHOTO_DB="${SELFPHPHOTO_DB:-${SELFPHPHOTO_DATA_DIR}/selfphoto.db}"

# ===== sudo/root チェック =====
if [[ "${EUID:-$(id -u)}" -ne 0 ]]; then
  echo "エラー: root 権限で実行してください（sudo ./install.sh）" >&2
  exit 1
fi

echo "=== selfphoto インストール: ${SELFPHPHOTO_HOME} ==="

# ===== Python 確認 =====
if ! command -v python3 >/dev/null 2>&1; then
  echo "エラー: python3 が見つかりません（Python 3.10+ が必要です）" >&2
  exit 1
fi
echo "python3: $(command -v python3) ($(python3 -c 'import sys; print("%d.%d" % sys.version_info[:2])'))"

# ===== Pillow 依存の解決 =====
# あればそのまま使う。無ければ主要なパッケージマネージャで自動導入する。
if python3 -c "import PIL" 2>/dev/null; then
  echo "Pillow: 既にインストール済み ($(python3 -c 'import PIL; print(PIL.__version__)'))"
else
  echo "Pillow が無いためインストールします..."
  if command -v apt-get >/dev/null 2>&1; then
    apt-get update -qq && apt-get install -y -qq python3-pil
  elif command -v dnf >/dev/null 2>&1; then
    dnf install -y python3-pillow
  elif command -v yum >/dev/null 2>&1; then
    yum install -y python3-pillow
  elif command -v apk >/dev/null 2>&1; then
    apk add --no-cache py3-pillow
  elif command -v pacman >/dev/null 2>&1; then
    pacman -Sy --noconfirm python-pillow
  elif command -v zypper >/dev/null 2>&1; then
    zypper --non-interactive install python3-Pillow
  else
    echo "警告: 対応するパッケージマネージャが見つかりません。" >&2
    echo "  手動で Pillow を導入してください: pip3 install Pillow" >&2
  fi
  if python3 -c "import PIL" 2>/dev/null; then
    echo "OK: Pillow 導入完了"
  else
    echo "警告: Pillow 無しで続行します（Exif の精度が落ちます）"
  fi
fi

# ===== プログラム配置 =====
# リポジトリ直下（install.sh と program/ が並ぶ場所）でも、
# program/ 配下（program/install.sh）でも実行できるようにする。
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
if [[ -d "${SCRIPT_DIR}/program/selfphoto" ]]; then
  SRC_PROGRAM="${SCRIPT_DIR}/program"
elif [[ -d "${SCRIPT_DIR}/selfphoto" ]]; then
  SRC_PROGRAM="${SCRIPT_DIR}"
else
  echo "エラー: selfphoto パッケージが見つかりません（リポジトリ一式の中で実行してください）" >&2
  exit 1
fi

install -d -m 755 "${SELFPHPHOTO_HOME}"
install -d -m 755 "${SELFPHPHOTO_HOME}/program"

# selfphoto パッケージと systemd ユニットを置き換え（DB selfphoto.db は保持）
# 注意: リポジトリを /opt/selfphoto に直接 clone して ./install.sh する
# ドキュメント通りの手順では、コピー元 (SRC_PROGRAM) と配置先
# (${SELFPHPHOTO_HOME}/program) が同一になる。このまま rm -rf すると
# ソース自身を消してしまい「cp: cannot stat ... No such file or directory」
# で起動できなくなるため、同一パスではコピーをスキップする。
DEST_PROGRAM="${SELFPHPHOTO_HOME}/program"
if [[ "${SRC_PROGRAM%/}" == "${DEST_PROGRAM%/}" ]]; then
  echo "インプレース実行のためプログラムのコピーをスキップします (${DEST_PROGRAM})"
else
  rm -rf "${SELFPHPHOTO_HOME}/program/selfphoto" "${SELFPHPHOTO_HOME}/program/systemd"
  cp -a "${SRC_PROGRAM}/selfphoto" "${SELFPHPHOTO_HOME}/program/"
  cp -a "${SRC_PROGRAM}/systemd" "${SELFPHPHOTO_HOME}/program/"
fi
find "${SELFPHPHOTO_HOME}/program" -name '__pycache__' -type d -prune -exec rm -rf {} + 2>/dev/null || true

# アイコン（ファビコン / apple-touch-icon）があれば配置
# インプレース実行（SCRIPT_DIR == 配置先）では同一ファイルなのでスキップする。
if [[ -d "${SCRIPT_DIR}/icon" && "${SCRIPT_DIR%/}" != "${SELFPHPHOTO_HOME%/}" ]]; then
  mkdir -p "${SELFPHPHOTO_HOME}/icon"
  cp -a "${SCRIPT_DIR}/icon/." "${SELFPHPHOTO_HOME}/icon/"
fi

# README / LICENSE / uninstall.sh があれば /opt/selfphoto/ にも配置
# インプレース実行ではコピー元と配置先が同一ファイルなのでスキップする。
for f in README.md LICENSE uninstall.sh; do
  if [[ "${SCRIPT_DIR%/}" == "${SELFPHPHOTO_HOME%/}" && -f "${SELFPHPHOTO_HOME}/$f" ]]; then
    continue
  fi
  if [[ -f "${SCRIPT_DIR}/$f" ]]; then
    cp -a "${SCRIPT_DIR}/$f" "${SELFPHPHOTO_HOME}/"
  elif [[ -f "${SRC_PROGRAM}/$f" ]]; then
    cp -a "${SRC_PROGRAM}/$f" "${SELFPHPHOTO_HOME}/"
  fi
done
if [[ -f "${SELFPHPHOTO_HOME}/uninstall.sh" ]]; then
  chmod 755 "${SELFPHPHOTO_HOME}/uninstall.sh"
fi

# ===== データ・写真・サムネイル用ディレクトリを事前に作成 =====
# mkdir -p は既存フォルダがあっても削除・上書きしない（中身はそのまま保持される）。
# server 起動時に DB・写真ディレクトリが無いと起動失敗するため、移行処理より先に作る。
mkdir -p "${SELFPHPHOTO_DATA_DIR}" "${SELFPHPHOTO_PHOTO_DIR}" "${SELFPHPHOTO_THUMB_DIR}" "${SELFPHPHOTO_EDIT_DIR}"
chmod 755 "${SELFPHPHOTO_DATA_DIR}" "${SELFPHPHOTO_PHOTO_DIR}" "${SELFPHPHOTO_THUMB_DIR}" "${SELFPHPHOTO_EDIT_DIR}"

# ===== 旧配置のデータ移行（プログラム領域に置きっぱなしになっている場合） =====
# 旧バージョンは写真・サムネイル・DB を /opt/selfphoto 配下に置いていたため、
# あればデータ領域へ移動する（写真データがワークスペース配下に残らないようにする）。
OLD_DATA="${SELFPHPHOTO_HOME}/selfphoto-data"
OLD_THUMB="${SELFPHPHOTO_HOME}/thumbnail"
OLD_DB="${SELFPHPHOTO_HOME}/program/selfphoto.db"
NEW_DB="${SELFPHPHOTO_DATA_DIR}/selfphoto.db"
if [[ -d "${OLD_DATA}" && ! -L "${OLD_DATA}" ]]; then
  if [[ -d "${OLD_DATA}/2026" || -n "$(ls -A "${OLD_DATA}" 2>/dev/null)" ]]; then
    echo "旧配置の写真データを移動: ${OLD_DATA} -> ${SELFPHPHOTO_DATA_DIR}"
    mkdir -p "${SELFPHPHOTO_DATA_DIR}"
    cp -a "${OLD_DATA}/." "${SELFPHPHOTO_DATA_DIR}/"
    mv "${OLD_DATA}" "${OLD_DATA}.migrated"
  else
    rmdir "${OLD_DATA}" 2>/dev/null || true
  fi
fi
if [[ -d "${OLD_THUMB}" && ! -L "${OLD_THUMB}" ]]; then
  if [[ -n "$(ls -A "${OLD_THUMB}" 2>/dev/null)" ]]; then
    echo "旧配置のサムネイルを移動: ${OLD_THUMB} -> ${SELFPHPHOTO_THUMB_DIR}"
    mkdir -p "${SELFPHPHOTO_THUMB_DIR}"
    cp -a "${OLD_THUMB}/." "${SELFPHPHOTO_THUMB_DIR}/"
    mv "${OLD_THUMB}" "${OLD_THUMB}.migrated"
  else
    rmdir "${OLD_THUMB}" 2>/dev/null || true
  fi
fi
if [[ -f "${OLD_DB}" && ! -e "${NEW_DB}" ]]; then
  echo "旧配置の DB を移動: ${OLD_DB} -> ${NEW_DB}"
  mkdir -p "$(dirname "${NEW_DB}")"
  for extra in "${OLD_DB}-wal" "${OLD_DB}-shm"; do
    if [[ -f "$extra" ]]; then
      cp -a "$extra" "${NEW_DB}${extra##${OLD_DB}}"
    fi
  done
  mv "${OLD_DB}" "${OLD_DB}.migrated"
  rm -f "${OLD_DB}-wal" "${OLD_DB}-shm"
fi

# ===== 旧レイアウトの移行（年フォルダがデータ直下にあった時代の構成） =====
# 2025/, 2026/ といった年フォルダを photo/ 配下へ移す。
# DB の path は photo ルートからの相対パスなので、フォルダを動かすだけで DB はそのまま使える。
shopt -s nullglob
year_dirs=("${SELFPHPHOTO_DATA_DIR}"/[12][0-9][0-9][0-9])
if ((${#year_dirs[@]})); then
  echo "旧レイアウトの年フォルダを photo/ 配下へ移動します: ${year_dirs[*]}"
  mkdir -p "${SELFPHPHOTO_PHOTO_DIR}"
  for d in "${year_dirs[@]}"; do
    year="$(basename "$d")"
    if [[ -e "${SELFPHPHOTO_PHOTO_DIR}/${year}" ]]; then
      echo "  統合: ${d} -> ${SELFPHPHOTO_PHOTO_DIR}/${year}"
      cp -a "${d}/." "${SELFPHPHOTO_PHOTO_DIR}/${year}/"
      mv "$d" "${d}.migrated"
    else
      echo "  移動: ${d} -> ${SELFPHPHOTO_PHOTO_DIR}/${year}"
      mv "$d" "${SELFPHPHOTO_PHOTO_DIR}/${year}"
    fi
  done
fi
shopt -u nullglob

# ===== データ・写真・サムネイル・DB ディレクトリ作成（念のため再確認） =====
# mkdir -p は既存フォルダがあっても削除しない。既存データ・DB は触らない（更新インストールでも保持される）
mkdir -p "${SELFPHPHOTO_DATA_DIR}" "${SELFPHPHOTO_PHOTO_DIR}" "${SELFPHPHOTO_THUMB_DIR}" "${SELFPHPHOTO_EDIT_DIR}"
chmod 755 "${SELFPHPHOTO_DATA_DIR}" "${SELFPHPHOTO_PHOTO_DIR}" "${SELFPHPHOTO_THUMB_DIR}" "${SELFPHPHOTO_EDIT_DIR}"

# ===== systemd ユニット登録 =====
if command -v systemctl >/dev/null 2>&1 && [[ -d /etc/systemd/system ]]; then
  cp "${SELFPHPHOTO_HOME}/program/systemd"/selfphoto-*.service /etc/systemd/system/
  cp "${SELFPHPHOTO_HOME}/program/systemd"/selfphoto-*.timer /etc/systemd/system/
  systemctl daemon-reload
  systemctl enable --now selfphoto-server.service
  systemctl enable --now selfphoto-scan.timer selfphoto-thumbs.timer
else
  echo
  echo "systemd が使えない環境です。手動起動は次のコマンドで:"
  echo "  cd ${SELFPHPHOTO_HOME}/program && python3 -m selfphoto.server"
fi

# ===== tailscale serve で公開 =====
# tailscale 接続済みの環境では 3360 で selfphoto を公開する。
# 再実行しても同じ設定の上書きになる。失敗してもインストール自体は続行する。
# 明示的に無効化したい場合は SELFPHPHOTO_SKIP_TAILSCALE_SERVE=1 を付けて実行する。
TAILSCALE_SERVED=0
if [[ "${SELFPHPHOTO_SKIP_TAILSCALE_SERVE:-0}" == "1" ]]; then
  echo "tailscale serve による公開をスキップします (SELFPHPHOTO_SKIP_TAILSCALE_SERVE=1)"
elif command -v tailscale >/dev/null 2>&1 && tailscale status >/dev/null 2>&1; then
  echo "tailscale serve で公開します: https://<tailnet>:3360 -> http://127.0.0.1:3360"
  if tailscale serve --bg --https=3360 http://127.0.0.1:3360; then
    TAILSCALE_SERVED=1
  else
    echo "警告: tailscale serve の設定に失敗しました。手動で実行してください:" >&2
    echo "  tailscale serve --bg --https=3360 http://127.0.0.1:3360" >&2
  fi
else
  echo "tailscale 未接続のため公開をスキップします（接続後に手動で公開できます）:"
  echo "  tailscale serve --bg --https=3360 http://127.0.0.1:3360"
fi

# ===== 完了 =====
echo
echo "=== インストール完了 ==="
echo "配置先       : ${SELFPHPHOTO_HOME}"
echo "データ       : ${SELFPHPHOTO_DATA_DIR}"
echo "写真         : ${SELFPHPHOTO_PHOTO_DIR}"
echo "サムネイル   : ${SELFPHPHOTO_THUMB_DIR}"
echo "編集写真     : ${SELFPHPHOTO_EDIT_DIR}"
echo "DB           : ${SELFPHPHOTO_DB}"
echo
echo "バックアップ  : ${SELFPHPHOTO_PHOTO_DIR} をコピーするだけで写真は全部取れます"
echo
if command -v systemctl >/dev/null 2>&1 && [[ -d /etc/systemd/system ]]; then
  echo "=== 状態 ==="
  systemctl --no-pager --lines=0 status selfphoto-server.service || true
  systemctl list-timers 'selfphoto-*' --no-pager || true
  echo
fi
echo "Web UI        : http://127.0.0.1:3360"
# Tailnet 経由のアクセス先 URL を表示する（実アドレスは実行時に動的取得し、埋め込まない）
TAIL_ADDR=""
TAIL_DNS=""
if command -v tailscale >/dev/null 2>&1; then
  TAIL_ADDR="$(tailscale ip -4 2>/dev/null | head -n 1 | tr -d '[:space:]' || true)"
  TAIL_DNS="$(tailscale status --json 2>/dev/null | python3 -c 'import json,sys; d=json.load(sys.stdin); print(d.get("Self",{}).get("DNSName","").rstrip("."))' 2>/dev/null || true)"
fi
echo
if [[ "${TAILSCALE_SERVED}" == "1" && -n "${TAIL_DNS}" ]]; then
  echo "アクセス先     : https://${TAIL_DNS}:3360/ （tailscale serve で公開中）"
elif [[ "${TAILSCALE_SERVED}" == "1" && -n "${TAIL_ADDR}" ]]; then
  echo "アクセス先     : https://${TAIL_ADDR}:3360/ （tailscale serve で公開中）"
elif [[ -n "${TAIL_DNS}" ]]; then
  echo "アクセス先     : https://${TAIL_DNS}:3360"
elif [[ -n "${TAIL_ADDR}" ]]; then
  echo "アクセス先     : https://${TAIL_ADDR}:3360"
else
  echo "アクセス先     : https://<tailnetアドレス>:3360"
  echo "  （tailscale に接続後に 'tailscale ip -4' で表示されるアドレスに置き換えてください）"
fi
echo
echo "tailscale serve の公開をやり直す場合:"
echo "  tailscale serve --bg --https=3360 http://127.0.0.1:3360"
echo "公開しない場合のインストール:"
echo "  sudo SELFPHPHOTO_SKIP_TAILSCALE_SERVE=1 -E ./install.sh"
echo
echo "アンインストール: sudo ${SELFPHPHOTO_HOME}/uninstall.sh または リポジトリの uninstall.sh"
