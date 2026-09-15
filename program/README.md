# selfphoto

docker 不要のセルフホスト写真管理。Immich のような「新しい順」タイムラインを
Tailscale 経由で公開する。

## 構成

```
/opt/selfphoto/              # プログラム専用（データは置かない）
└── program/          # このプログラム一式（Python 標準ライブラリのみで動作）
    └── selfphoto/
        ├── common.py   # 設定・SQLite スキーマ
        ├── exif.py     # 撮影日時の抽出（Pillow があれば高精度、無くても JPEG/TIFF は対応）
        ├── ingest.py   # スキャン／サムネイル生成／SDカード等からの取り込み
        └── server.py   # Web サーバ + Web UI

/opt/lxd-data/selfphoto-data/   # 写真・サムネイル・DB（プログラム領域の外）
├── photo/                      # ★写真の本体（バックアップはこのフォルダだけ）
│   └── 2026/202609/20260911_/IMG_0001.jpg  # 年/年月/年月日_ フォルダに自動整理
├── thumbnail/2026/202609/20260911__IMG_0001_thumb.webp  # サムネイル（photo/ と同じ階層構造）
└── selfphoto.db                # SQLite DB（WAL）
```

写真・サムネイル・DB はプログラム領域の外に置くため、プログラム領域ごと
ワークスペースに取り込んでも写真データが含まれることはない。
バックアップは `photo/` だけコピーすればよい（サムネイル・DB は再生成可能）。
- ポート: 127.0.0.1:3360（tailscale serve で公開）
- 依存: Python 3.10+。Pillow（推奨, `apt install python3-pil`）。HEIC のデコードには
  `pillow-heif` が必要（無い場合はファイル日時で整理される）。

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
クリックで拡大（← → キーで前後移動、動画は再生）。

ヘッダー右のボタン:

- **アップロード** : ファイル選択からアップロード（ドラッグ＆ドロップも可）
- **選択** : 選択モード。写真・日付フォルダ・月見出しの左上にチェックボックスが
  表示され、複数選択できる（見出しのチェックで配下をまとめて選択）
- **ダウンロード** : 選択した写真をダウンロード。フォルダ内の写真をすべて
  選択した場合はフォルダ単位で zip 圧縮、それ以外は 1 枚ずつダウンロード

## systemd で常時化

```bash
sudo cp /opt/selfphoto/program/systemd/*.{service,timer} /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now selfphoto-server.service
sudo systemctl enable --now selfphoto-scan.timer selfphoto-thumbs.timer
```

- `selfphoto-server.service` : Web サーバ（127.0.0.1:3360）
- `selfphoto-scan.timer`     : 10 分ごとにデータフォルダをスキャン
- `selfphoto-thumbs.timer`   : 5 分ごとに未生成サムネイルを処理

## tailscale serve で公開

```bash
tailscale serve --bg --https=3360 http://127.0.0.1:3360
```

Tailnet 内の `https://<tailnetアドレス>:3360/` でアクセスできる。
（旧バージョン構文: `tailscale serve https / http://127.0.0.1:3360`）

特定マシンだけでなく tailnet 全体に公開する場合は:

```bash
tailscale serve --bg --https=3360,http://127.0.0.1:3360
```

## 設定（環境変数）

| 変数 | 既定値 | 説明 |
|---|---|---|
| `SELFPHPHOTO_DATA_DIR` | `/opt/lxd-data/selfphoto-data` | DB・サムネイル等のデータの置き場所 |
| `SELFPHPHOTO_PHOTO_DIR` | `/opt/lxd-data/selfphoto-data/photo` | 写真の保存先（バックアップ対象） |
| `SELFPHPHOTO_THUMB_DIR` | `/opt/lxd-data/selfphoto-data/thumbnail` | サムネイル保存先 |
| `SELFPHPHOTO_DB` | `/opt/lxd-data/selfphoto-data/selfphoto.db` | SQLite DB |
| `SELFPHPHOTO_HOST` | `127.0.0.1` | 待ち受けアドレス |
| `SELFPHPHOTO_PORT` | `3360` | ポート |

## API

- `GET /api/photos?limit=500&offset=0&month=202609` — 新しい順の写真リスト
- `GET /api/search?q=キーワード` — ファイル名・カメラ名・パスの部分一致検索
- `POST /api/upload` — multipart 一括アップロード（manifest フィールドで各ファイルの lastModified を渡せる）
- `GET /api/months` — 月ごとの件数
- `GET /api/zip?prefix=<photo/ からの相対フォルダ>&name=<zip名>` — フォルダを zip 圧縮してダウンロード
- `POST /api/import` — サーバ上のフォルダから取り込む（JSON `{"src": "/media/usb/DCIM"}`。バックグラウンド実行）
- `GET /api/import-status` — 取り込みの状態・前回結果
- `POST /api/restart` — selfphoto-server.service を再起動（systemd 環境のみ）
- `POST /api/update` — GitHub から最新版を取得して更新（systemd 環境では続けて再起動）
- `GET /thumb/<相対パス>_thumb.webp` — サムネイル
- `GET /photo/<相対パス>` — オリジナル（Range 対応、動画シーク可）
- `GET /healthz` — ヘルスチェック
