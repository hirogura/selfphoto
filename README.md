# selfphoto

セルフホストできる写真管理ソフトです。
Tailscale 経由で公開します（バージョン: v.0.0.8 — `common.py` の `VERSION` で管理、
Web UI 左上に表示）。

依存は Python 3.10+ の標準ライブラリのみ（Pillow は推奨）。インストールスクリプト
`install.sh` 1 本で、任意の Linux 環境に `/opt/selfphoto` へ導入できます。

写真・サムネイル・DB はプログラム領域 (`/opt/selfphoto`) とは切り離し、
`/opt/lxd-data/selfphoto-data` 配下に置いています（写真データがワークスペース／配布物に
混入して誤ってアップロードされることがないようにするため）
さらに写真の本体は `photo/` 配下に集約しているので、**バックアップは
`/opt/lxd-data/selfphoto-data/photo` だけをコピーすればよいはずです**。

## 構成

```
/opt/selfphoto/              # プログラム専用（データは置かない）
├── program/
│   └── selfphoto/
│       ├── common.py   # 設定・SQLite スキーマ
│       ├── exif.py     # 撮影日時の抽出（Pillow があれば高精度、無くても JPEG/TIFF は対応）
│       ├── ingest.py   # スキャン／サムネイル生成／SDカード等からの取り込み
│       └── server.py   # Web サーバ + Web UI
└── icon/              # ファビコン・apple-touch-icon（Web UI が配信）

/opt/lxd-data/selfphoto-data/   # 写真・サムネイル・DB（プログラム領域の外）
├── photo/                      # ★写真の本体（バックアップはこのフォルダだけ）
│   └── 2026/202609/20260911_/IMG_0001.jpg  # 年/年月/年月日_ フォルダに自動整理
├── thumbnail/2026/202609/20260911__IMG_0001_thumb.webp  # サムネイル（photo/ と同じ階層構造）
└── selfphoto.db                # SQLite DB（WAL）
```

- ポート: 127.0.0.1:3360（tailscale serve で公開）
- 依存: Python 3.10+。Pillow（推奨, `apt install python3-pil`）。HEIC のデコードには
  `pillow-heif` が必要（無い場合はファイル日時で整理される）。

## インストール

### 必要要件

- Linux + systemd（systemd が無い環境でも手動起動は可能）
- Python 3.10 以上
- Pillow（推奨。無い場合はスクリプトが自動導入を試みる）

### 手順

初回:

```bash
cd /opt
git clone https://github.com/hirogura/selfphoto.git
cd selfphoto
sudo ./install.sh
```

2 回目以降（更新・再インストール）は `/opt/selfphoto` が既にあるため
`git clone` せずに `git pull` で更新してから実行する
（`git clone` すると `fatal: destination path 'selfphoto' already exists`
で失敗する）:

```bash
cd /opt/selfphoto
git pull
sudo ./install.sh
```

`install.sh` は次を行う:

1. root 権限チェック
2. python3 / Pillow の確認（無ければ apt / dnf / yum / apk / pacman / zypper で自動導入）
3. プログラム一式を `/opt/selfphoto/program` へ配置
   （DB はデータ領域にあるため、再実行＝アップデートになる）
4. 旧レイアウト（データ直下の年フォルダ）があれば `photo/` 配下へ自動移行
5. `/opt/lxd-data/selfphoto-data`（DB・写真・サムネイル）を作成（既存データは保持）
6. systemd ユニット登録 + サーバ・タイマーを有効化して起動
7. tailscale 接続済みなら `tailscale serve --bg --https=3360` で自動公開
   （`SELFPHPHOTO_SKIP_TAILSCALE_SERVE=1` で無効化可）

配置先を変えたい場合:

```bash
sudo SELFPHPHOTO_HOME=/srv/selfphoto -E ./install.sh
```

systemd の無い環境では、ユニット登録をスキップして手動起動の手順を表示する。

### systemd ユニット

- `selfphoto-server.service` : Web サーバ（127.0.0.1:3360）
- `selfphoto-scan.timer`     : 10 分ごとにデータフォルダをスキャン
- `selfphoto-thumbs.timer`   : 5 分ごとに未生成サムネイルを処理

### tailscale serve で公開

`install.sh` が tailscale 接続済みの環境では自動で公開する
（`https://<tailnetアドレス>:3360/` → `http://127.0.0.1:3360`）。
手動でやり直す場合:

```bash
tailscale serve --bg --https=3360 http://127.0.0.1:3360
```

公開せずにインストールする場合:

```bash
sudo SELFPHPHOTO_SKIP_TAILSCALE_SERVE=1 -E ./install.sh
```

Tailnet 内の `https://<tailnetアドレス>:3360/` でアクセスできる。

特定マシンだけでなく tailnet 全体に公開する場合は:

```bash
tailscale serve --bg --https=3360,http://127.0.0.1:3360
```

## 使い方

```bash
cd /opt/selfphoto/program

# SD カード等から取り込み（Exif の日付で 年/年月/年月日_ へコピーして登録）
python3 -m selfphoto.ingest import /media/usb/DCIM

# すでに selfphoto-data に写真がある場合はスキャンして DB 登録
python3 -m selfphoto.ingest scan

# サムネイル生成（未生成のみ）
python3 -m selfphoto.ingest thumbs

# サーバ起動（開発・確認用。常時は systemd を使う）
python3 -m selfphoto.server
```

Web UI: Immich 風のレイアウト。左サイドバー（写真／検索／アップロード、
左下に「再起動」ボタン — 押すと selfphoto-server.service を再起動、
「アップデート」ボタン — 押すと GitHub から最新版を取得して更新）、
新しい順のタイムライン（月見出し＋日付見出し＋グリッド）、
右端のスクロールインジケータ（年月バー・クリックでジャンプ、スクロール中は
見ている年月フォルダを表示）、ドラッグ＆ドロップ／複数ファイル一括アップロード
（対応形式: jpg/png/heic/webp/avif/tiff/bmp/gif と主要動画）、
 クリックで拡大（← → キーで前後移動、動画は再生。右上のボタンで
 ダウンロード／削除も可能）。

ヘッダー右のボタン:

- **アップロード** : ファイル選択からアップロード（ドラッグ＆ドロップも可）
- **選択** : 選択モード。写真・日付フォルダ・月見出しの左上にチェックボックスが
  表示され、複数選択できる（見出しのチェックで配下をまとめて選択）
- **ダウンロード** : 選択した写真をダウンロード。日付フォルダ・月見出しを
  明示チェックした場合はフォルダ単位で zip 圧縮、ファイル個別チェックの
  場合は全ファイル選択でも 1 枚ずつダウンロード
- **削除** : 選択した写真を削除（一覧・ファイル実体・サムネイル。確認あり）

## 設定（環境変数）

| 変数 | 既定値 | 説明 |
|---|---|---|
| `SELFPHPHOTO_PROGRAM_DIR` | `/opt/selfphoto/program` | プログラム配置先 |
| `SELFPHPHOTO_DATA_DIR` | `/opt/lxd-data/selfphoto-data` | DB・サムネイル等のデータの置き場所 |
| `SELFPHPHOTO_PHOTO_DIR` | `/opt/lxd-data/selfphoto-data/photo` | 写真の保存先（バックアップ対象） |
| `SELFPHPHOTO_THUMB_DIR` | `/opt/lxd-data/selfphoto-data/thumbnail` | サムネイル保存先 |
| `SELFPHPHOTO_DB` | `/opt/lxd-data/selfphoto-data/selfphoto.db` | SQLite DB |
| `SELFPHPHOTO_HOST` | `127.0.0.1` | 待ち受けアドレス |
| `SELFPHPHOTO_PORT` | `3360` | ポート |
| `SELFPHPHOTO_MAX_UPLOAD_GB` | `20` | アップロード 1 リクエストの最大サイズ (GB) |

## API

- `GET /api/photos?limit=500&offset=0&month=202609` — 新しい順の写真リスト
- `GET /api/search?q=キーワード` — ファイル名・カメラ名・パスの部分一致検索
- `POST /api/upload` — multipart 一括アップロード（manifest フィールドで各ファイルの lastModified を渡せる）
- `GET /api/months` — 月ごとの件数
- `GET /api/zip?prefix=<photo/ からの相対フォルダ>&name=<zip名>` — フォルダを zip 圧縮してダウンロード
- `POST /api/delete` — 写真を削除（JSON `{"paths": [...]}`。ファイル実体・サムネイル・DB 行）
- `POST /api/restart` — selfphoto-server.service を再起動（systemd 環境のみ）
- `POST /api/update` — GitHub から最新版を取得して更新（systemd 環境では続けて再起動）
- `GET /thumb/<相対パス>_thumb.webp` — サムネイル
- `GET /photo/<相対パス>` — オリジナル（Range 対応、動画シーク可）
- `GET /healthz` — ヘルスチェック

## アンインストール

写真データ (`/opt/lxd-data/selfphoto-data/photo`) は削除しない（`--purge` で削除）:

```bash
sudo ./uninstall.sh
```

### 手動で行う場合

```bash
# 1. サービス停止・無効化
sudo systemctl disable --now selfphoto-server.service
sudo systemctl disable --now selfphoto-scan.timer selfphoto-thumbs.timer

# 2. ユニットファイル削除
sudo rm /etc/systemd/system/selfphoto-*.service /etc/systemd/system/selfphoto-*.timer
sudo systemctl daemon-reload
sudo systemctl reset-failed 'selfphoto-*' 2>/dev/null || true

# 3. プログラム削除（写真データ /opt/lxd-data/selfphoto-data/photo/ は残る）
sudo rm -rf /opt/selfphoto/program /opt/selfphoto/README.md /opt/selfphoto/LICENSE /opt/selfphoto/uninstall.sh

# 4. データごと完全に削除する場合
sudo rm -rf /opt/selfphoto
sudo rm -rf /opt/lxd-data/selfphoto-data
```

## ライセンス

MIT License — 詳細は [LICENSE](LICENSE) を参照。
# selfphoto
