"""selfphoto 共通設定・DB スキーマ."""
from __future__ import annotations

import os
import sqlite3
import threading
from pathlib import Path

VERSION = "1.7.2"

# ディレクトリ設定
# 写真・サムネイル・DB はプログラム領域 (/opt/selfphoto) と完全に分離し、
# /opt/lxd-data 側に置く（ワークスペース外なので誤ってアップロードされない）。
# 写真の本体は DATA_DIR/photo 配下に集約し、photo/ だけコピーすればバックアップになる。
PROGRAM_DIR = Path(os.environ.get("SELFPHPHOTO_PROGRAM_DIR", "/opt/selfphoto/program"))
DATA_DIR = Path(os.environ.get("SELFPHPHOTO_DATA_DIR", "/opt/lxd-data/selfphoto-data"))
PHOTO_DIR = Path(os.environ.get("SELFPHPHOTO_PHOTO_DIR", str(DATA_DIR / "photo")))
THUMB_DIR = Path(os.environ.get("SELFPHPHOTO_THUMB_DIR", str(DATA_DIR / "thumbnail")))
# ビューア用プレビュー画像（長辺 VIEW_SIZE px に縮小）を置く場所。
# 一覧サムネイル（512px）では粗いがオリジナルは重い、という中間を担う。
VIEW_DIR = Path(os.environ.get("SELFPHPHOTO_VIEW_DIR", str(DATA_DIR / "thumbnail-view")))
# 編集画像フォルダ（編集モードの「編集フォルダに保存」の保存先。一覧とは分離）
EDIT_PHOTO_DIR = Path(os.environ.get("SELFPHPHOTO_EDIT_DIR", str(DATA_DIR / "edit-photo")))
DB_PATH = Path(os.environ.get("SELFPHPHOTO_DB", str(DATA_DIR / "selfphoto.db")))

# サーバ設定
HOST = os.environ.get("SELFPHPHOTO_HOST", "127.0.0.1")
PORT = int(os.environ.get("SELFPHPHOTO_PORT", "3360"))

# アップロード 1 リクエストの最大サイズ
MAX_UPLOAD = int(os.environ.get("SELFPHPHOTO_MAX_UPLOAD_GB", "20")) * 1024 ** 3

# 拡張子（撮影データは非対応）
PHOTO_EXTS = {".jpg", ".jpeg", ".png", ".heic", ".heif", ".webp", ".avif", ".tif", ".tiff", ".bmp", ".gif"}
VIDEO_EXTS = {".mp4", ".mov", ".m4v", ".avi", ".mkv", ".webm", ".3gp", ".mts", ".m2ts", ".wmv"}

SUPPORTED_EXTS = PHOTO_EXTS | VIDEO_EXTS

# サムネイルは年/年月/ベース名.webp
THUMB_SIZE = 512
THUMB_EXT = ".webp"

# ビューア用プレビューは長辺 1280px の WebP
VIEW_SIZE = 1280
VIEW_EXT = ".webp"
VIEW_QUALITY = 80

_local = threading.local()


def get_db() -> sqlite3.Connection:
    """スレッドごとの sqlite3 接続を返す（WAL モード）。"""
    conn = getattr(_local, "conn", None)
    if conn is None:
        conn = sqlite3.connect(DB_PATH, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA foreign_keys=ON")
        _local.conn = conn
    return conn


SCHEMA = """
CREATE TABLE IF NOT EXISTS photos (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    path          TEXT UNIQUE NOT NULL,       -- PHOTO_DIR からの相対パス
    filename      TEXT NOT NULL,
    captured_at   TEXT NOT NULL,              -- UTC ISO8601 (Exif 欠損は mtime)
    captured_local TEXT NOT NULL,             -- ローカル ISO8601 (UI 表示・フォルダ分割用)
    year          TEXT NOT NULL,
    month         TEXT NOT NULL,              -- YYYYMM
    is_video      INTEGER NOT NULL DEFAULT 0,
    width         INTEGER,
    height        INTEGER,
    camera        TEXT,
    size          INTEGER NOT NULL DEFAULT 0,
    mtime         REAL NOT NULL DEFAULT 0,
    hash          TEXT NOT NULL,
    thumb_path    TEXT,
    thumb_done    INTEGER NOT NULL DEFAULT 0,
    created_at    TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_photos_captured ON photos(captured_at DESC, id DESC);
CREATE INDEX IF NOT EXISTS idx_photos_month ON photos(month);
CREATE INDEX IF NOT EXISTS idx_photos_thumb_done ON photos(thumb_done);
"""


def init_db() -> None:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = get_db()
    conn.executescript(SCHEMA)
    conn.commit()
