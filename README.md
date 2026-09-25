# selfphoto

セルフホストできる写真管理ソフトです。
Tailscale 経由で公開します（バージョン: v.1.7.6 — `common.py` の `VERSION` で管理、
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
├── edit-photo/                 # 編集モードで保存した画像（サイドバー「編集写真」で表示）
└── selfphoto.db                # SQLite DB（WAL）
```

- ポート: 127.0.0.1:3360（tailscale serve で公開）
- 依存: Python 3.10+。Pillow（推奨, `apt install python3-pil`）。HEIC のデコードには
  `pillow-heif` が必要（無い場合はファイル日時で整理される）。
  バックアップ機能には `rsync` が必要（`install.sh` が自動導入を試みる）。

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
5. `/opt/lxd-data/selfphoto-data`（DB・写真・サムネイル・編集画像）を作成（既存データは保持）
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

Web UI: 馴染みやすいレイアウト。左サイドバー（写真／編集写真／検索／
バックアップ／インポート／複数選択／アップロード。写真・編集写真の右に件数を表示。
検索時はサイドバーに検索欄が現れる。
左下に「再起動」ボタン — 押すと selfphoto-server.service を再起動、
「アップデート」ボタン — 押すと GitHub から最新版を取得して更新）、
上部ツールバーは無く、月見出しがそのまま先頭に吸着する
新しい順のタイムライン（月見出し＋日付見出し＋グリッド）、
右端のスクロールインジケータ（年月バー・クリックでジャンプ、スクロール中は
 見ている年月フォルダを表示）、ドラッグ＆ドロップ／複数ファイル一括アップロード
（対応形式: jpg/png/heic/webp/avif/tiff/bmp/gif と主要動画。
右下のアップロードマネージャに逐次表示 — ファイルごとの進捗・
処理中・完了／スキップ（重複）／エラー、全体バー・速度・残り時間、
キャンセル・失敗分の再試行・最小化に対応）、
  クリックで拡大（画面内に収まる縮小表示が既定。← → キーで前後移動、動画は再生。
   写真は左レールの「拡大」「縮小」ボタン（`+` / `-` キー）でズーム可能。
   拡大表示中は画像のドラッグで表示位置を移動できる。
   ダブルクリックで一覧に戻る。
   左レールには上から拡大・％表示（クリックで画面に合わせる）・縮小・
   コピー／編集／削除／DLが並ぶ。写真は「編集」ボタンで編集モードに移行）。
 コピーはクリップボードへ画像を書き込み（iPhone の Safari は PNG のみ
 対応のため、長辺2048に縮小した PNG としてコピー）。

サイドバーのボタン:

- **インポート** : 押すと下に取り込み欄が表示される。
  上段の **取込** は既存の selfphoto-data からの取り込み
  （`python3 -m selfphoto.ingest scan` と同じ。インストールし直した時などに使う）。
  下段はサーバ上のフォルダを指定して **取込** を押すと
  `python3 -m selfphoto.ingest import` と同じ取り込みを実行する
  （いずれもバックグラウンド実行、結果は欄内に表示）
- **複数選択** : 選択モード。写真・日付フォルダ・月見出しの左上にチェックボックスが
  表示され、複数選択できる（見出しのチェックで配下をまとめて選択）。
  押すと下に **ダウンロード**・**削除** が表示される（もう一度押すと解除）
- **ダウンロード** : 選択した写真をダウンロード。日付フォルダ・月見出しを
  明示チェックした場合はフォルダ単位で zip 圧縮、ファイル個別チェックの
  場合は全ファイル選択でも 1 枚ずつダウンロード
- **削除** : 選択した写真を削除（一覧・ファイル実体・サムネイル。確認あり）
- **アップロード** : ファイル選択からアップロード（ドラッグ＆ドロップも可）

サムネイル一覧の右クリックメニュー（動画はダウンロード・削除のみ）:

- **左回転 / 右回転** : 90度回転し、サムネイルと元画像を修正。EXIF は維持して上書き保存
- **コピー** : 元画像をクリップボードへコピー
- **編集** : 対象の画像で編集画面に移行
- **ダウンロード** : 元画像をダウンロード
- **削除** : 対象を削除（一覧・ファイル実体・サムネイル。確認あり）

## 画像編集

写真ビューアの右上「編集」ボタンで編集モードに移行（動画は対象外）。
右側に編集ボタンを縦に表示：

- **左回転 / 右回転** : 押すたびに90度回転
- **赤枠挿入** : ドラッグした範囲に角丸の赤枠。太さ 5 段階
- **矢印挿入** : ドラッグした方向・長さで赤矢印。太さ 5 段階
- **トリミング** : 画像上でドラッグして範囲選択。「比率維持」（元画像と同じ縦横比）
  ・「自由選択」・「4:3」・「3:2」・「16:9」。固定比率の選択中はその比率で選択される。「適用」で切り抜き
- **モザイク** : 筆ツールで塗った場所にモザイク。強度 5 段階・太さ 5 段階
- **ぼかし** : 筆ツールで塗った場所をぼかし。強度 5 段階・太さ 5 段階
  （選択中はカーソルが太さと同じ直径の円になる）
- **シャドウ / ハイライト / コントラスト / 彩度 / 色温度 / 色合い** :
  スライダー（-100〜+100、中央 0）でリアルタイム調整。
  シャドウは暗部のみ・ハイライトは明部のみ明るく（右で明るく）、
  コントラストは右で強調、彩度は右で濃く、
  色温度は右で高く（暖かく）、色合いは右でマゼンタ寄り（Lightroom と同じ向き）
- **リサイズ** : 長辺・横幅・縦幅のいずれかを指定（縦横比は維持）。
  横幅プリセット 1980 / 1280 / 1024 / 320 付き
- **リネーム** : 拡張子より前のファイル名部分を変更（DB・サムネイルも追従）
- **1つ戻す** : 直前の操作を取り消し（最大20手前まで。筆操作は1ストローク単位）

保存方法（png→PNG・webp→WebP で元形式を維持、それ以外は JPEG で書き出し）：

- **上書き保存** : 元ファイルを置き換え（DB・サムネイルも更新）
- **別名保存** : 通常の取り込みとして新規登録（`元の名前_edit`＋元形式の拡張子）
- **編集フォルダに保存** : `edit-photo/` に保存。サイドバー「編集写真」で表示

## バックアップ

サイドバー「バックアップ」で rsync によるコピー設定を行う
（[rsyncgui](https://github.com/hirogura/rsyncgui.git) と同じ方式）。
設定は `/opt/lxd-data/selfphoto-data/backup.json` に保存される
（パスワードを含むためパーミッション 600。リポジトリには含まれない）。

- **ソースフォルダ** : 既定は写真フォルダ全体。`photo/2026` のように絞っても可。
  `.upload-tmp/` は常に除外される
- **ターゲットフォルダ** : ローカルパスまたは SSH 有効時はリモートパス。
  「フォルダ確認」で存在確認。無い場合は自動作成（ローカル・リモートとも `mkdir -p`）
- **SSHリモート接続** : ホスト・ユーザー・ポート・鍵ファイル・パスワードを指定
  （パスワード認証には `sshpass` が必要。`install.sh` が自動導入する。鍵認証を推奨）。
  「接続確認」で SSH 疎通だけをテスト（接続エラー切り分け用）、「設定保存」で保存。
  `sshpass` が未導入の場合は「インストールしますか？」の確認後に自動導入し、再確認する。
  apt update 失敗時もキャッシュのまま install を試し、失敗時は update スキップ再試行の
  確認表示と対処ヒント（Read-only 時の手動導入・鍵認証への切替など）を表示する。
  失敗時は実行ユーザー・鍵・sshpass の診断と対処ヒントを表示する。
  注意: SSH を実行するのはサーバー本体（root）のため、ターミナルで使える鍵・
  `~/.ssh/config`・ssh-agent はそのままでは使われない。
  鍵認証の場合はサーバー用の鍵を作って転送先に登録し、鍵ファイルに絶対パスを指定する
  （例: `sudo ssh-keygen -f /root/.ssh/id_ed25519 -N ""` →
  `sudo ssh-copy-id -i /root/.ssh/id_ed25519.pub user@バックアップ先ホスト`）。
- **オプション固定** : `-r`（再帰） `-t`（時刻維持） `-u`（新しいもののみ）
  `-v`（詳細） `--progress`（進捗）
- **コピー実行** : 今すぐコピー（バックグラウンド実行、状態表示で進捗確認）
- **監視開始** : 保存・取込で写真が増えたら、選択した間隔（1/5/15/30/60分、
  既定5分）で自動コピー。変更がなければ実行しないため、保存のたびに
  全体走査する方式より軽い。サーバー再起動後も設定どおり復帰する。
  Web以外（スキャン・CLI取込）で増えた分も検知する。間隔の変更は保存時に即時反映される

## 設定（環境変数）

| 変数 | 既定値 | 説明 |
|---|---|---|
| `SELFPHPHOTO_PROGRAM_DIR` | `/opt/selfphoto/program` | プログラム配置先 |
| `SELFPHPHOTO_DATA_DIR` | `/opt/lxd-data/selfphoto-data` | DB・サムネイル等のデータの置き場所 |
| `SELFPHPHOTO_PHOTO_DIR` | `/opt/lxd-data/selfphoto-data/photo` | 写真の保存先（バックアップ対象） |
| `SELFPHPHOTO_THUMB_DIR` | `/opt/lxd-data/selfphoto-data/thumbnail` | サムネイル保存先 |
| `SELFPHPHOTO_EDIT_DIR` | `/opt/lxd-data/selfphoto-data/edit-photo` | 編集画像フォルダ |
| `SELFPHPHOTO_DB` | `/opt/lxd-data/selfphoto-data/selfphoto.db` | SQLite DB |
| `SELFPHPHOTO_HOST` | `127.0.0.1` | 待ち受けアドレス |
| `SELFPHPHOTO_PORT` | `3360` | ポート |
| `SELFPHPHOTO_MAX_UPLOAD_GB` | `20` | アップロード 1 リクエストの最大サイズ (GB) |

## API

- `GET /api/photos?limit=500&offset=0&month=202609` — 新しい順の写真リスト
- `GET /api/search?q=キーワード` — ファイル名・カメラ名・パスの部分一致検索
- `POST /api/upload` — multipart 一括アップロード（manifest フィールドで各ファイルの lastModified を渡せる。結果に `duplicate`・`duplicates` を含む）
- `GET /api/months` — 月ごとの件数・写真総数・編集写真件数
- `GET /api/zip?prefix=<photo/ からの相対フォルダ>&name=<zip名>` — フォルダを zip 圧縮してダウンロード
- `POST /api/delete` — 写真を削除（JSON `{"paths": [...]}`。ファイル実体・サムネイル・DB 行。`edit/` prefix で編集フォルダ内も可）
- `GET /api/edits` — 編集画像フォルダの一覧
- `POST /api/edit-save` — 編集結果を編集フォルダに保存（multipart）
- `POST /api/edit-overwrite` — 編集結果で上書き保存（multipart `path` + ファイル）
- `POST /api/rename` — ファイル名を変更（JSON `{"path", "name"}`。拡張子は維持、DB・サムネイルも追従）
- `POST /api/rotate` — 元画像を90度回転して上書き保存（JSON `{"path", "dir": "left"|"right"}`。EXIF維持、サムネイル・DBも更新。動画は対象外）
- `GET /api/backup-config` — バックアップ設定を取得（パスワードはマスク）
- `POST /api/backup-config` — バックアップ設定を保存
- `POST /api/backup-run` — バックアップを今すぐ実行（バックグラウンド。ターゲットが無ければ自動作成）
- `POST /api/backup-ssh-test` — SSH接続だけ確認（接続エラー切り分け用）
- `POST /api/backup-sshpass-install` — `sshpass` をサーバー側に自動導入（未導入時の確認用。`{"skipUpdate": true}` で apt update を省略して再試行可）
- `POST /api/backup-target-check` — ターゲットフォルダ確認。無ければ作成（`mkdir -p`）
- `GET /api/backup-status` — バックアップの状態・前回結果
- `POST /api/backup-watch` — 監視の開始・停止（JSON `{"enabled": true}`）
- `POST /api/import` — サーバ上のフォルダから取り込む（JSON `{"src": "/media/usb/DCIM"}`。バックグラウンド実行）
- `GET /api/import-status` — 取り込みの状態・前回結果
- `POST /api/scan` — 既存の selfphoto-data から取り込む（バックグラウンド実行）
- `GET /api/scan-status` — scan の状態・前回結果
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
