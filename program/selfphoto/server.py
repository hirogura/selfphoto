"""selfphoto Web サーバ（標準ライブラリのみ / docker 不要）。

  python3 -m selfphoto.server

tailscale serve 前提なので 127.0.0.1 のみで待ち受ける。
"""
from __future__ import annotations

import html
import json
import mimetypes
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse, unquote, quote

from . import common

mimetypes.add_type("image/webp", ".webp")
mimetypes.add_type("image/heic", ".heic")
mimetypes.add_type("image/avif", ".avif")
mimetypes.add_type("video/mp4", ".mp4")
mimetypes.add_type("video/quicktime", ".mov")
mimetypes.add_type("image/png", ".png")
mimetypes.add_type("image/x-icon", ".ico")

# アイコン類（プログラム領域の icon/ に置いたものをそのまま配信する）
ICON_DIR = Path(os.environ.get("SELFPHPHOTO_ICON_DIR", str(common.PROGRAM_DIR.parent / "icon")))
ICON_FILES = {
    "/apple-touch-icon.png": (ICON_DIR / "apple-touch-icon.png", "image/png"),
    "/favicon.ico": (ICON_DIR / "selfphotofav.png", "image/png"),
    "/icon/selfphotofav.png": (ICON_DIR / "selfphotofav.png", "image/png"),
}

SAFE_REL = re.compile(r"^[\w][\w\-./ ]*$")


def safe_join(base: Path, rel: str) -> Path | None:
    """base の外へ出ない相対パスだけ許可する。"""
    if not rel or ".." in rel.split("/") or "\x00" in rel:
        return None
    p = (base / rel).resolve()
    try:
        p.relative_to(base.resolve())
    except ValueError:
        return None
    return p


class _BodyReader:
    """Content-Length で区切られたボディを読む軽量リーダー（unshift 対応）。"""

    def __init__(self, rfile, length: int):
        self.rfile = rfile
        self.remaining = length
        self.buf = b""

    def read(self, n: int = -1) -> bytes:
        if n == -1:
            n = self.remaining + len(self.buf)
        if n <= 0:
            return b""
        if n <= len(self.buf):
            out, self.buf = self.buf[:n], self.buf[n:]
            return out
        out = self.buf
        self.buf = b""
        while len(out) < n and self.remaining > 0:
            chunk = self.rfile.read(min(262144, self.remaining))
            if not chunk:
                break
            self.remaining -= len(chunk)
            need = n - len(out)
            out += chunk[:need]
            rest = chunk[need:]
            if rest:
                self.buf = rest
                break
        return out

    def readline(self) -> bytes:
        while True:
            i = self.buf.find(b"\n")
            if i >= 0:
                line = self.buf[:i + 1]
                self.buf = self.buf[i + 1:]
                return line
            if self.remaining <= 0:
                line, self.buf = self.buf, b""
                return line
            chunk = self.rfile.read(min(8192, self.remaining))
            if not chunk:
                line, self.buf = self.buf, b""
                return line
            self.remaining -= len(chunk)
            self.buf += chunk

    def unshift(self, data: bytes) -> None:
        self.buf = data + self.buf


def stream_multipart(reader: "_BodyReader", boundary: bytes):
    """multipart/form-data をストリーム解析する。

    (name, filename, payload) を返すジェネレータ。
    filename=None の通常フィールドは payload が str、
    ファイル部分は payload が SpooledTemporaryFile（大きい場合はディスク退避）。
    """
    delim = b"--" + boundary
    # 最初の boundary までスキップ（preamble 無視）
    while True:
        line = reader.readline()
        if not line:
            return
        if line.strip() == delim:
            break
    while True:
        headers = {}
        while True:
            line = reader.readline()
            if line in (b"\r\n", b"\n", b""):
                break
            k, _, v = line.decode("utf-8", "replace").partition(":")
            headers[k.strip().lower()] = v.strip()
        disp = headers.get("content-disposition", "")
        name_m = re.search(r'name="([^"]*)"', disp)
        file_m = re.search(r'filename="([^"]*)"', disp)
        if name_m is None:
            return
        name = name_m.group(1)
        filename = file_m.group(1) if file_m else None
        end = b"\r\n" + delim
        tail = b""
        if filename is None:
            value = b""
            while True:
                chunk = reader.read(65536)
                if not chunk:
                    value += tail
                    break
                tail += chunk
                i = tail.find(end)
                if i >= 0:
                    value += tail[:i]
                    reader.unshift(tail[i + len(end):])
                    break
                keep = len(end) - 1
                if len(tail) > keep:
                    value += tail[:-keep]
                    tail = tail[-keep:]
            yield name, None, value.decode("utf-8", "replace")
        else:
            fh = tempfile.SpooledTemporaryFile(max_size=8 * 1024 * 1024)
            while True:
                chunk = reader.read(262144)
                if not chunk:
                    fh.write(tail)
                    break
                tail += chunk
                i = tail.find(end)
                if i >= 0:
                    fh.write(tail[:i])
                    reader.unshift(tail[i + len(end):])
                    break
                keep = len(end) - 1
                if len(tail) > keep:
                    fh.write(tail[:-keep])
                    tail = tail[-keep:]
            fh.seek(0)
            yield name, filename, fh
        # デリミタ直後: \r\n(次パート) か --(終端)
        after = reader.readline()
        if not after or after.startswith(b"--"):
            return


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "selfphoto/1.4.0"

    # ------------------------------------------------------------------
    def log_message(self, fmt, *args):  # 静かにする
        if os.environ.get("SELFPHPHOTO_VERBOSE"):
            super().log_message(fmt, *args)

    def send_json(self, obj, code: int = 200) -> None:
        body = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def send_file(self, path: Path, download_name: str | None = None,
                  ctype: str | None = None, cache_days: int | None = None) -> None:
        try:
            size = path.stat().st_size
        except OSError:
            self.send_error(404)
            return
        if ctype is None:
            ctype = mimetypes.guess_type(str(path))[0] or "application/octet-stream"
        rng = self.headers.get("Range")
        start, end = 0, size - 1
        code = 200
        if rng:
            m = re.match(r"bytes=(\d*)-(\d*)$", rng.strip())
            if m and (m.group(1) or m.group(2)):
                if m.group(1):
                    start = int(m.group(1))
                    if m.group(2):
                        end = min(int(m.group(2)), size - 1)
                else:
                    start = max(0, size - int(m.group(2)))
                if start > end or start >= size:
                    self.send_response(416)
                    self.send_header("Content-Range", f"bytes */{size}")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                code = 206
        length = end - start + 1
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(length))
        self.send_header("Accept-Ranges", "bytes")
        if cache_days is not None:
            self.send_header("Cache-Control", f"private, max-age={cache_days * 86400}")
        else:
            self.send_header("Cache-Control", "private, max-age=86400")
        if code == 206:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        if download_name:
            self.send_header(
                "Content-Disposition",
                f"attachment; filename*=UTF-8''{quote(download_name)}",
            )
        self.end_headers()
        if self.command == "HEAD":
            return
        with path.open("rb") as f:
            f.seek(start)
            remaining = length
            while remaining > 0:
                chunk = f.read(min(256 * 1024, remaining))
                if not chunk:
                    break
                try:
                    self.wfile.write(chunk)
                except (BrokenPipeError, ConnectionResetError):
                    return
                remaining -= len(chunk)

    # ------------------------------------------------------------------
    def do_HEAD(self) -> None:
        self.do_GET()

    def do_GET(self) -> None:
        try:
            self.route()
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as e:  # noqa: BLE001
            try:
                self.send_json({"error": str(e)}, 500)
            except Exception:
                pass

    def do_POST(self) -> None:
        try:
            parsed = urlparse(self.path)
            path = unquote(parsed.path)
            if path == "/api/upload":
                self.api_upload()
            elif path == "/api/restart":
                self.api_restart()
            elif path == "/api/update":
                self.api_update()
            elif path == "/api/delete":
                self.api_delete()
            elif path == "/api/edit-save":
                self.api_edit_save()
            elif path == "/api/edit-overwrite":
                self.api_edit_overwrite()
            elif path == "/api/edit-saveas":
                self.api_edit_saveas()
            elif path == "/api/rename":
                self.api_rename()
            elif path == "/api/rotate":
                self.api_rotate()
            elif path == "/api/backup-run":
                self.api_backup_run()
            elif path == "/api/backup-watch":
                self.api_backup_watch()
            elif path == "/api/backup-config":
                self.api_backup_save()
            elif path == "/api/backup-ssh-test":
                self.api_backup_ssh_test()
            elif path == "/api/backup-target-check":
                self.api_backup_target_check()
            else:
                self.send_json({"error": "not found"}, 404)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as e:  # noqa: BLE001
            try:
                self.send_json({"error": str(e)}, 500)
            except Exception:
                pass

    def api_upload(self) -> None:
        from . import ingest
        from . import common as C

        if (self.headers.get("Content-Type") or "").lower().startswith("application/x-www-form-urlencoded"):
            self.send_json({"error": "multipart/form-data required"}, 400)
            return
        ctype = self.headers.get("Content-Type", "")
        m = re.search(r'boundary=([^;]+)', ctype)
        if "multipart/form-data" not in ctype.lower() or not m:
            self.send_json({"error": "multipart/form-data required"}, 400)
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = 0
        if length <= 0:
            self.send_json({"error": "empty body"}, 400)
            return
        if length > C.MAX_UPLOAD:
            self.send_json({"error": "payload too large"}, 413)
            return
        boundary = m.group(1).strip().strip('"').encode()
        reader = _BodyReader(self.rfile, length)
        results, errors = [], []
        conn = C.get_db()
        try:
            # 1 周目: パートを収集（manifest フィールドで各ファイルの lastModified を受け取る）
            parts, manifest = [], {}
            for field_name, filename, payload in stream_multipart(reader, boundary):
                if filename is None:
                    if field_name == "manifest":
                        try:
                            manifest = json.loads(payload)
                        except Exception:
                            manifest = {}
                    continue
                if not filename:
                    continue
                parts.append((filename, payload))
            for filename, payload in parts:
                if payload.seek(0, 2) > C.MAX_UPLOAD:
                    payload.close()
                    errors.append({"name": filename, "error": "too large"})
                    continue
                payload.seek(0)
                # Exif が無い場合のフォールバック: ブラウザが送るファイル更新日 (ms)
                fallback_ts = None
                lm = manifest.get(filename)
                if lm is None:
                    lm = self.headers.get("X-File-Last-Modified")
                if lm is not None:
                    try:
                        fallback_ts = float(lm) / 1000.0
                    except (TypeError, ValueError):
                        fallback_ts = None
                try:
                    dest, duplicate = ingest.save_upload(filename, payload, fallback_ts=fallback_ts)
                    pid = ingest.finalize_upload(dest, fallback_ts=fallback_ts)
                    results.append({"name": filename, "path": dest.relative_to(C.PHOTO_DIR).as_posix(), "id": pid, "duplicate": bool(duplicate)})
                except Exception as e:  # noqa: BLE001
                    errors.append({"name": filename, "error": str(e)})
                finally:
                    payload.close()
        except Exception as e:  # noqa: BLE001
            self.send_json({"error": f"upload failed: {e}", "results": results, "errors": errors}, 500)
            return
        if results:
            try:
                from . import backup
                backup.mark_dirty()
            except Exception:
                pass
        self.send_json({"ok": True, "count": len(results),
                        "duplicates": sum(1 for r in results if r.get("duplicate")),
                        "results": results, "errors": errors})

    def route(self) -> None:
        parsed = urlparse(self.path)
        path = unquote(parsed.path)
        if path == "/" or path.startswith("/index"):
            self.send_html()
        elif path in ICON_FILES:
            p, ctype = ICON_FILES[path]
            if p.is_file():
                self.send_file(p, ctype=ctype, cache_days=7)
            else:
                self.send_json({"error": "icon not found"}, 404)
        elif path == "/api/photos":
            self.api_photos(parsed.query)
        elif path == "/api/edits":
            self.api_edits()
        elif path == "/api/backup-config":
            self.api_backup_config()
        elif path == "/api/backup-status":
            self.api_backup_status()
        elif path == "/api/months":
            self.api_months()
        elif path == "/api/search":
            self.api_search(parsed.query)
        elif path.startswith("/thumb/"):
            self.serve_thumb(path[len("/thumb/"):])
        elif path.startswith("/view/"):
            self.serve_view(path[len("/view/"):])
        elif path.startswith("/editthumb/"):
            self.serve_editthumb(path[len("/editthumb/"):])
        elif path.startswith("/editphoto/"):
            self.serve_editphoto(path[len("/editphoto/"):])
        elif path.startswith("/photo/"):
            self.serve_photo(path[len("/photo/"):])
        elif path == "/api/zip":
            self.api_zip(parsed.query)
        elif path == "/api/restart":
            self.api_restart()
        elif path == "/healthz":
            self.send_json({"status": "ok", "time": time.time()})
        else:
            self.send_json({"error": "not found"}, 404)

    def api_restart(self) -> None:
        """selfphoto-server.service を再起動する（systemd 環境のみ）。

        レスポンスを返してから systemctl restart を発行する。
        """
        unit = os.environ.get("SELFPHPHOTO_RESTART_UNIT", "selfphoto-server.service")
        if not (os.path.isdir("/run/systemd/system") and shutil.which("systemctl")):
            self.send_json({"ok": False, "error": "systemd not available"}, 400)
            return
        self.send_json({"ok": True, "restarting": unit})
        # レスポンス送信を確実に完了させてから再起動する
        try:
            self.wfile.flush()
        except OSError:
            pass
        subprocess.Popen(
            ["systemctl", "restart", unit],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True,
        )

    def api_update(self) -> None:
        """GitHub から最新版を取得して install.sh で更新する。

        clone から install.sh 実行まで丸ごと一時ユニット内で行う。
        理由: サーバ本体は ProtectSystem=strict + PrivateTmp で動いており、
        (1) 子プロセスのままでは /opt/selfphoto 等が read-only で書けない、
        (2) サーバ側の /tmp はプライベート名前空間のため一時ユニットと
        共有できない。制限なしの一時ユニット内で完結させれば両方回避できる。
        （プログラム一式の上書き・systemd ユニット再登録。写真・DB は保持）。
        systemd 環境ではレスポンス後に selfphoto-server.service を再起動して
        新しいコードを読み込ませる。
        """
        import shlex

        repo = os.environ.get(
            "SELFPHPHOTO_UPDATE_REPO", "https://github.com/hirogura/selfphoto.git")
        home = os.environ.get("SELFPHPHOTO_HOME", str(common.PROGRAM_DIR.parent))
        use_unit = (os.path.isdir("/run/systemd/system")
                    and shutil.which("systemctl")
                    and shutil.which("systemd-run")
                    and shutil.which("git"))
        try:
            if use_unit:
                # 一時ユニット内で clone → install.sh を実行する。
                # 終了コード・出力は --pipe/--wait で回収する。
                script = (
                    "set -e\n"
                    'TMP=$(mktemp -d /tmp/selfphoto-update-XXXXXX)\n'
                    'trap \'rm -rf "$TMP"\' EXIT\n'
                    f"git clone --depth 1 {shlex.quote(repo)} \"$TMP/repo\"\n"
                    'cd "$TMP/repo"\n'
                    "test -f install.sh\n"
                    f"SELFPHPHOTO_HOME={shlex.quote(home)} bash ./install.sh\n"
                )
                inst = subprocess.run(
                    ["systemd-run", "--pipe", "--wait", "--collect",
                     "-p", "ProtectSystem=no",
                     "bash", "-c", script],
                    capture_output=True, text=True, timeout=900,
                )
                if inst.returncode != 0:
                    tail = (inst.stdout.strip() + "\n" + inst.stderr.strip()).strip()[-2000:]
                    self.send_json({"ok": False,
                                    "error": f"install.sh failed: {tail}"}, 500)
                    return
            else:
                import tempfile

                if not shutil.which("git"):
                    self.send_json({"ok": False, "error": "git not found"}, 500)
                    return
                with tempfile.TemporaryDirectory(prefix="selfphoto-update-") as tmp:
                    clone = subprocess.run(
                        ["git", "clone", "--depth", "1", repo, "repo"],
                        cwd=tmp, capture_output=True, text=True, timeout=300,
                    )
                    if clone.returncode != 0:
                        self.send_json({"ok": False,
                                        "error": f"git clone failed: {clone.stderr.strip()}"}, 500)
                        return
                    repo_dir = Path(tmp) / "repo"
                    if not (repo_dir / "install.sh").is_file():
                        self.send_json({"ok": False, "error": "install.sh not found in repo"}, 500)
                        return
                    env = dict(os.environ, SELFPHPHOTO_HOME=home)
                    inst = subprocess.run(
                        ["bash", "install.sh"], cwd=repo_dir,
                        capture_output=True, text=True, timeout=600, env=env,
                    )
                    if inst.returncode != 0:
                        tail = (inst.stderr.strip() or inst.stdout.strip())[-2000:]
                        self.send_json({"ok": False,
                                        "error": f"install.sh failed: {tail}"}, 500)
                        return
        except subprocess.TimeoutExpired:
            self.send_json({"ok": False, "error": "update timed out"}, 504)
            return
        except Exception as e:  # noqa: BLE001
            self.send_json({"ok": False, "error": str(e)}, 500)
            return
        unit = os.environ.get("SELFPHPHOTO_RESTART_UNIT", "selfphoto-server.service")
        if not (os.path.isdir("/run/systemd/system") and shutil.which("systemctl")):
            self.send_json({"ok": True, "restarting": None,
                            "note": "updated; restart manually (systemd not available)"})
            return
        self.send_json({"ok": True, "restarting": unit})
        try:
            self.wfile.flush()
        except OSError:
            pass
        subprocess.Popen(
            ["systemctl", "restart", unit],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True,
        )

    # ------------------------------------------------------------------
    # APIs
    # ------------------------------------------------------------------
    def api_photos(self, query: str) -> None:
        from urllib.parse import parse_qs

        from . import ingest

        q = parse_qs(query)
        limit = min(int(q.get("limit", ["500"])[0]), 2000)
        offset = max(int(q.get("offset", ["0"])[0]), 0)
        month = q.get("month", [None])[0]
        conn = common.get_db()
        sql = ("SELECT id, path, filename, captured_at, captured_local, is_video,"
               " width, height, camera, size, mtime, thumb_done FROM photos")
        params: list = []
        if month and re.match(r"^\d{6}$", month):
            sql += " WHERE month=?"
            params.append(month)
        sql += " ORDER BY captured_at DESC, id DESC LIMIT ? OFFSET ?"
        params += [limit, offset]
        rows = conn.execute(sql, params).fetchall()
        photos = []
        base_url = os.environ.get("SELFPHPHOTO_PUBLIC_URL", "").rstrip("/")
        for r in rows:
            rel = r["path"]
            # mtime をクエリに付けてキャッシュバスティングする。
            # 上書き保存後は同じURLだとブラウザのキャッシュ(86400秒)が
            # 古い画像を出し続けるため。
            v = int(r["mtime"] or 0)
            thumb = f"/thumb/{Path(rel).with_suffix('').as_posix()}_thumb.webp?v={v}" if r["thumb_done"] == 1 else None
            if base_url:
                thumb = base_url + thumb if thumb else None
            # ビューア用プレビュー（長辺1280px）。動画は対象外。
            # ファイルが無くても serve 時に遅延生成されるため URL は付与する。
            view = f"/view/{ingest.view_rel_path(rel)}?v={v}" if not r["is_video"] else None
            photos.append({
                "id": r["id"],
                "path": rel,
                "filename": r["filename"],
                "capturedAt": r["captured_at"],
                "capturedLocal": r["captured_local"],
                "isVideo": bool(r["is_video"]),
                "width": r["width"],
                "height": r["height"],
                "camera": r["camera"],
                "size": r["size"],
                "thumb": thumb,
                "view": view,
                "original": f"/photo/{rel}?v={v}",
            })
        self.send_json({"photos": photos, "count": len(photos)})

    def api_search(self, query: str) -> None:
        from urllib.parse import parse_qs

        from . import ingest

        q = parse_qs(query)
        term = (q.get("q", [""])[0] or "").strip()
        limit = min(int(q.get("limit", ["500"])[0]), 2000)
        offset = max(int(q.get("offset", ["0"])[0]), 0)
        if not term:
            self.send_json({"photos": [], "count": 0})
            return
        like = f"%{term}%"
        conn = common.get_db()
        rows = conn.execute(
            "SELECT id, path, filename, captured_at, captured_local, is_video,"
            " width, height, camera, size, mtime, thumb_done FROM photos"
            " WHERE filename LIKE ? OR camera LIKE ? OR path LIKE ?"
            " ORDER BY captured_at DESC, id DESC LIMIT ? OFFSET ?",
            (like, like, like, limit, offset),
        ).fetchall()
        photos = []
        for r in rows:
            rel = r["path"]
            v = int(r["mtime"] or 0)
            thumb = f"/thumb/{Path(rel).with_suffix('').as_posix()}_thumb.webp?v={v}" if r["thumb_done"] == 1 else None
            view = f"/view/{ingest.view_rel_path(rel)}?v={v}" if not r["is_video"] else None
            photos.append({
                "id": r["id"], "path": rel, "filename": r["filename"],
                "capturedAt": r["captured_at"], "capturedLocal": r["captured_local"],
                "isVideo": bool(r["is_video"]), "width": r["width"], "height": r["height"],
                "camera": r["camera"], "size": r["size"], "thumb": thumb,
                "view": view,
                "original": f"/photo/{rel}?v={v}",
            })
        self.send_json({"photos": photos, "count": len(photos)})

    def api_months(self) -> None:
        conn = common.get_db()
        rows = conn.execute(
            "SELECT year, month, COUNT(*) AS n FROM photos GROUP BY month ORDER BY month DESC"
        ).fetchall()
        months = [{"year": r["year"], "month": r["month"], "count": r["n"]} for r in rows]
        total = conn.execute("SELECT COUNT(*) AS n FROM photos").fetchone()["n"]
        # 編集写真フォルダの件数（/api/edits と同じ数え方）
        edits = 0
        try:
            base = common.EDIT_PHOTO_DIR
            if base.is_dir():
                edits = sum(1 for p in base.iterdir()
                            if p.is_file() and p.suffix.lower() in common.PHOTO_EXTS)
        except OSError:
            edits = 0
        self.send_json({"months": months, "total": total, "edits": edits})

    # ------------------------------------------------------------------
    # files
    # ------------------------------------------------------------------
    def serve_thumb(self, rel: str) -> None:
        p = safe_join(common.THUMB_DIR, rel)
        if p is None or not p.is_file():
            self.send_json({"error": "thumb not found"}, 404)
            return
        self.send_file(p)

    def serve_photo(self, rel: str) -> None:
        p = safe_join(common.PHOTO_DIR, rel)
        if p is None or not p.is_file():
            self.send_json({"error": "photo not found"}, 404)
            return
        self.send_file(p)

    def _ensure_view(self, rel: str):
        """ビューア用プレビュー画像を用意する（VIEW_DIR 配下にキャッシュ）。

        rel はプレビュー側の相対パス（…_view.webp）。末尾を剥がして
        元画像（… + 写真拡張子）を探し、無い・または元画像より古い場合は
        その場で生成する。
        動画・生成失敗時は None（呼び出し側はオリジナルにフォールバック）。
        """
        from . import ingest

        stem = rel
        suffix = "_view" + common.VIEW_EXT
        if stem.endswith(suffix):
            stem = stem[: -len(suffix)]
        if not stem or ".." in stem.split("/") or "\x00" in stem:
            return None
        # 実ファイルの拡張子は大文字（.JPG等）の場合があるため、
        # 親フォルダを走査して大文字小文字を無視して探す。
        if "/" in stem:
            dir_rel, base = stem.rsplit("/", 1)
            parent = safe_join(common.PHOTO_DIR, dir_rel)
        else:
            dir_rel, base = "", stem
            parent = common.PHOTO_DIR
        if parent is None or not parent.is_dir():
            return None
        want = {base.lower() + ext for ext in common.PHOTO_EXTS}
        src = None
        try:
            for p in parent.iterdir():
                if p.is_file() and p.name.lower() in want:
                    src = p
                    break
        except OSError:
            return None
        if src is None:
            return None
        orig_rel = f"{dir_rel}/{src.name}" if dir_rel else src.name
        if src.suffix.lower() in common.VIDEO_EXTS:
            return None
        dst_rel = ingest.view_rel_path(orig_rel)
        dst = safe_join(common.VIEW_DIR, dst_rel)
        if dst is None:
            return None
        try:
            if dst.is_file() and dst.stat().st_mtime >= src.stat().st_mtime:
                return dst
        except OSError:
            pass
        if ingest.make_view_image(src, dst):
            return dst
        return None

    def serve_view(self, rel: str) -> None:
        p = self._ensure_view(unquote(rel))
        if p is None:
            self.send_json({"error": "view not found"}, 404)
            return
        self.send_file(p)

    def api_zip(self, query: str) -> None:
        """photo/ 配下のフォルダを zip でダウンロードさせる（選択ダウンロード用）。

        GET /api/zip?prefix=2026/202609/20260911_&name=20260911_
        prefix は PHOTO_DIR からの相対フォルダ。name はダウンロード時の zip 名。
        """
        from urllib.parse import parse_qs

        q = parse_qs(query)
        prefix = (q.get("prefix", [""])[0] or "").strip("/")
        base = safe_join(common.PHOTO_DIR, prefix) if prefix else common.PHOTO_DIR
        if base is None or not base.is_dir():
            self.send_json({"error": "folder not found"}, 404)
            return
        name = q.get("name", [""])[0] or base.name or "photos"
        zip_name = f"{re.sub(r'[\\\\/:*?\"<>|]', '_', name)}.zip"
        self.send_response(200)
        self.send_header("Content-Type", "application/zip")
        self.send_header("Content-Disposition",
                         f"attachment; filename*=UTF-8''{quote(zip_name)}")
        # ストリーミングするため長さは不明（chunked）
        self.send_header("Transfer-Encoding", "chunked")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            for chunk in self._zip_chunks(base, prefix):
                self._write_chunk(chunk)
            self._write_chunk(b"")  # 終端
        except (BrokenPipeError, ConnectionResetError):
            pass

    def api_delete(self) -> None:
        """選択した写真を削除する（ファイル実体・サムネイル・DB 行）。

        POST /api/delete {"paths": ["2026/202609/20260911_/IMG_0001.jpg", ...]}
        paths は PHOTO_DIR からの相対パス。"edit/<名>" は編集フォルダ内を
        削除する（DB 行なし・サムネイルキャッシュ削除）。空になった
        日付フォルダは掃除する。
        """
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = 0
        if length <= 0 or length > 1024 * 1024:
            self.send_json({"error": "bad body"}, 400)
            return
        try:
            body = json.loads(self.rfile.read(length).decode("utf-8"))
        except Exception:
            self.send_json({"error": "bad json"}, 400)
            return
        rels = body.get("paths") if isinstance(body, dict) else None
        if not isinstance(rels, list) or not rels or len(rels) > 5000:
            self.send_json({"error": "paths required (1-5000)"}, 400)
            return
        conn = common.get_db()
        deleted, errors = [], []
        touched_dirs = set()
        for rel in rels:
            if not isinstance(rel, str):
                errors.append({"path": str(rel), "error": "bad path"})
                continue
            if rel.startswith("edit/"):
                # 編集フォルダ内のファイル（フラット配置のみ許可）
                name = rel[len("edit/"):]
                dst = safe_join(common.EDIT_PHOTO_DIR, name)
                if dst is None or "/" in name:
                    errors.append({"path": rel, "error": "bad path"})
                    continue
                try:
                    if dst.is_file():
                        dst.unlink()
                    cache = common.THUMB_DIR / "edit" / (Path(name).stem + "_thumb.webp")
                    if cache.is_file():
                        cache.unlink()
                    deleted.append(rel)
                except Exception as e:  # noqa: BLE001
                    errors.append({"path": rel, "error": str(e)})
                continue
            src = safe_join(common.PHOTO_DIR, rel)
            if src is None:
                errors.append({"path": rel, "error": "bad path"})
                continue
            # サムネイル（api_photos と同じ命名則: 元拡張子を除去 + _thumb.webp）
            thumb_rel = Path(rel).with_suffix("").as_posix() + "_thumb.webp"
            thumb = safe_join(common.THUMB_DIR, thumb_rel)
            # ビューア用プレビュー（ingest.view_rel_path と同じ命名則）
            view_rel = Path(rel).with_suffix("").as_posix() + "_view.webp"
            view = safe_join(common.VIEW_DIR, view_rel)
            try:
                if src.is_file():
                    touched_dirs.add(src.parent)
                    src.unlink()
                if thumb is not None and thumb.is_file():
                    touched_dirs.add(thumb.parent)
                    thumb.unlink()
                if view is not None and view.is_file():
                    touched_dirs.add(view.parent)
                    view.unlink()
                conn.execute("DELETE FROM photos WHERE path = ?", (rel,))
                deleted.append(rel)
            except Exception as e:  # noqa: BLE001
                errors.append({"path": rel, "error": str(e)})
        conn.commit()
        # 空になったフォルダを親方向へ掃除する（写真・サムネイル・プレビュー各側、データルート直下まで）
        for root in (common.PHOTO_DIR.resolve(), common.THUMB_DIR.resolve(),
                     common.VIEW_DIR.resolve()):
            for d in sorted(touched_dirs, key=lambda p: len(p.parts), reverse=True):
                try:
                    dp = d.resolve()
                except OSError:
                    continue
                while dp != root and root in dp.parents and dp.is_dir():
                    try:
                        if any(dp.iterdir()):
                            break
                        dp.rmdir()
                    except OSError:
                        break
                    dp = dp.parent
        self.send_json({"ok": True, "deleted": deleted, "errors": errors})

    # ------------------------------------------------------------------
    # edit-photo: 編集画像フォルダ（DATA_DIR/edit-photo）
    # ------------------------------------------------------------------
    def api_edits(self) -> None:
        """編集画像フォルダの一覧を写真ライクな形式で返す。

        path は "edit/<ファイル名>" 仮想prefix。一覧・viewer・選択UIで
        そのまま使える（削除は /api/delete が edit/ を受け付ける）。
        """
        from . import exif as exif_mod

        base = common.EDIT_PHOTO_DIR
        photos = []
        if base.is_dir():
            for p in sorted(base.iterdir()):
                if not p.is_file() or p.suffix.lower() not in common.PHOTO_EXTS:
                    continue
                try:
                    st = p.stat()
                    local = datetime.fromtimestamp(st.st_mtime).astimezone()
                except OSError:
                    continue
                try:
                    size = exif_mod.image_size(p)
                except Exception:
                    size = None
                photos.append({
                    "id": f"edit:{p.name}", "path": f"edit/{p.name}",
                    "filename": p.name,
                    "capturedAt": local.isoformat(), "capturedLocal": local.isoformat(),
                    "isVideo": False,
                    "width": size[0] if size else None,
                    "height": size[1] if size else None,
                    "camera": None, "size": st.st_size,
                    "thumb": f"/editthumb/{p.name}?v={int(st.st_mtime)}",
                    "original": f"/editphoto/{p.name}?v={int(st.st_mtime)}",
                })
        self.send_json({"photos": photos, "count": len(photos)})

    def serve_editphoto(self, rel: str) -> None:
        p = safe_join(common.EDIT_PHOTO_DIR, rel)
        if p is None or not p.is_file() or "/" in rel:
            self.send_json({"error": "edit photo not found"}, 404)
            return
        self.send_file(p)

    def _ensure_edit_thumb(self, name: str):
        """編集画像のサムネイルを用意する（THUMB_DIR/edit 配下にキャッシュ）。"""
        src = safe_join(common.EDIT_PHOTO_DIR, name)
        if src is None or not src.is_file() or "/" in name:
            return None
        dst = common.THUMB_DIR / "edit" / (Path(name).stem + "_thumb.webp")
        try:
            if dst.is_file() and dst.stat().st_mtime >= src.stat().st_mtime:
                return dst
        except OSError:
            pass
        try:
            from PIL import Image, ImageOps
        except ImportError:
            return None
        try:
            dst.parent.mkdir(parents=True, exist_ok=True)
            with Image.open(src) as im:
                im.draft("RGB", (common.THUMB_SIZE * 2, common.THUMB_SIZE * 2))
                try:
                    im.seek(0)
                except Exception:
                    pass
                # Exif Orientation を反映（縦写真は縦向きのサムネイルになる）
                try:
                    im = ImageOps.exif_transpose(im)
                except Exception:
                    pass
                if im is None:
                    return None
                im = im.convert("RGB")
                im.thumbnail((common.THUMB_SIZE, common.THUMB_SIZE), Image.LANCZOS)
                im.save(dst, "WEBP", quality=82, method=4)
            return dst
        except Exception:
            return None

    def serve_editthumb(self, rel: str) -> None:
        p = self._ensure_edit_thumb(unquote(rel))
        if p is None:
            # Pillow 無し等の場合はオリジナルをそのまま返す
            orig = safe_join(common.EDIT_PHOTO_DIR, unquote(rel))
            if orig is None or not orig.is_file() or "/" in unquote(rel):
                self.send_json({"error": "thumb not found"}, 404)
                return
            self.send_file(orig)
            return
        self.send_file(p)

    def _read_multipart_file(self):
        """multipart から (fields, filename, payload) を取り出す。"""
        from . import common as C

        if (self.headers.get("Content-Type") or "").lower().startswith("application/x-www-form-urlencoded"):
            return None, "multipart/form-data required"
        ctype = self.headers.get("Content-Type", "")
        m = re.search(r'boundary=([^;]+)', ctype)
        if "multipart/form-data" not in ctype.lower() or not m:
            return None, "multipart/form-data required"
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = 0
        if length <= 0:
            return None, "empty body"
        if length > C.MAX_UPLOAD:
            return None, "payload too large"
        boundary = m.group(1).strip().strip('"').encode()
        reader = _BodyReader(self.rfile, length)
        fields, filename, payload = {}, None, None
        for field_name, fname, part in stream_multipart(reader, boundary):
            if fname is None:
                if isinstance(part, str):
                    fields[field_name] = part
                continue
            if filename is None and fname:
                filename, payload = fname, part
            elif hasattr(part, "close"):
                part.close()
        if payload is None:
            return None, "file required"
        try:
            if payload.seek(0, 2) > C.MAX_UPLOAD:
                return None, "too large"
            payload.seek(0)
        except Exception:
            return None, "bad file"
        return (fields, filename, payload), None

    def api_edit_save(self) -> None:
        """編集結果を編集画像フォルダへ保存する（multipart file [+ name] [+ src]）。

        src（編集元の path）が分かれば、JPEG の場合のみ元画像の EXIF
        （撮影日時・メーカー・モデル）を引き継ぐ。
        DB 登録はしない（一覧は /api/edits がディスク走査で返す）。
        """
        from . import exif as exif_mod
        from . import ingest

        got, err = self._read_multipart_file()
        if got is None:
            self.send_json({"error": err}, 400)
            return
        fields, filename, payload = got
        try:
            name = ingest.sanitize_filename(fields.get("name") or filename or "edit")
            if not Path(name).suffix:
                name += ".jpg"
            dest_dir = common.EDIT_PHOTO_DIR
            dest_dir.mkdir(parents=True, exist_ok=True)
            dest = dest_dir / name
            n = 1
            while dest.exists():
                dest = dest_dir / f"{Path(name).stem}_{n}{Path(name).suffix}"
                n += 1
            with dest.open("wb") as out:
                shutil.copyfileobj(payload, out, length=4 * 1024 * 1024)
            # 編集元の EXIF が分かれば引き継ぐ（JPEG のみ）
            src_rel = (fields.get("src") or "").strip()
            if src_rel:
                if src_rel.startswith("edit/"):
                    sname = src_rel[len("edit/"):]
                    src_file = safe_join(common.EDIT_PHOTO_DIR, sname)
                    if src_file is None or "/" in sname:
                        src_file = None
                else:
                    src_file = safe_join(common.PHOTO_DIR, src_rel)
                if src_file is not None and src_file.is_file():
                    tags = exif_mod.edit_source_tags(src_file)
                    try:
                        head = dest.read_bytes()[:2]
                    except OSError:
                        head = b""
                    if tags.get("datetime") and head == b"\xff\xd8":
                        exif_mod.inject_exif_into_jpeg(
                            dest, tags["datetime"], tags.get("make"), tags.get("model"))
            try:
                from . import backup
                backup.mark_dirty()
            except Exception:
                pass
            self.send_json({"ok": True, "name": dest.name, "path": f"edit/{dest.name}"})
        except Exception as e:  # noqa: BLE001
            self.send_json({"error": str(e)}, 500)
        finally:
            try:
                payload.close()
            except Exception:
                pass

    def api_edit_overwrite(self) -> None:
        """編集結果で上書き保存する（multipart: path フィールド + file）。

        path が edit/ で始まれば編集フォルダ内を上書き（DB なし・
        サムネイルキャッシュ削除）。通常写真は実体を置き換えて DB
        （サイズ・ hash・縦横・サムネイル）を更新する。
        """
        from . import exif as exif_mod
        from . import ingest

        got, err = self._read_multipart_file()
        if got is None:
            self.send_json({"error": err}, 400)
            return
        fields, _filename, payload = got
        try:
            rel = (fields.get("path") or "").strip()
            if not rel:
                self.send_json({"error": "path required"}, 400)
                return
            data = payload.read()
            if rel.startswith("edit/"):
                name = rel[len("edit/"):]
                dst = safe_join(common.EDIT_PHOTO_DIR, name)
                if dst is None or "/" in name:
                    self.send_json({"error": "bad path"}, 400)
                    return
                dst.parent.mkdir(parents=True, exist_ok=True)
                old_tags = exif_mod.edit_source_tags(dst) if dst.is_file() else None
                dst.write_bytes(data)
                if old_tags and old_tags.get("datetime") and data[:2] == b"\xff\xd8":
                    exif_mod.inject_exif_into_jpeg(
                        dst, old_tags["datetime"], old_tags.get("make"), old_tags.get("model"))
                cache = common.THUMB_DIR / "edit" / (Path(name).stem + "_thumb.webp")
                try:
                    if cache.is_file():
                        cache.unlink()
                except OSError:
                    pass
                try:
                    from . import backup
                    backup.mark_dirty()
                except Exception:
                    pass
                self.send_json({"ok": True, "path": rel})
                return
            dst = safe_join(common.PHOTO_DIR, rel)
            if dst is None or not dst.is_file():
                self.send_json({"error": "photo not found"}, 404)
                return
            # 置き換え前に元画像の EXIF（撮影日時・メーカー・モデル）を読む
            src_tags = exif_mod.edit_source_tags(dst)
            tmp = dst.with_name(dst.name + ".edit-tmp")
            tmp.write_bytes(data)
            os.replace(tmp, dst)
            conn = common.get_db()
            try:
                size = exif_mod.image_size(dst)
            except Exception:
                size = None
            st = dst.stat()
            conn.execute(
                """UPDATE photos SET size=?, mtime=?, hash=?,
                          width=?, height=?, thumb_done=0 WHERE path=?""",
                (st.st_size, st.st_mtime, ingest.file_hash_of(dst),
                 size[0] if size else None, size[1] if size else None, rel),
            )
            conn.commit()
            # 古いサムネイルを消して即時再生成する（一覧の表示をすぐ最新化）
            row = conn.execute("SELECT * FROM photos WHERE path=?", (rel,)).fetchone()
            if row and row["thumb_path"]:
                old = safe_join(common.THUMB_DIR, row["thumb_path"])
                try:
                    if old is not None and old.is_file():
                        old.unlink()
                except OSError:
                    pass
            if row:
                ingest.make_thumbnail(row)
            # JPEG 出力なら EXIF（撮影日時・メーカー・モデル）を引き継ぐ。
            # captured_at（DB）は不変なので並び順も変わらない。
            try:
                head = dst.read_bytes()[:2]
            except OSError:
                head = b""
            if head == b"\xff\xd8":
                dt = src_tags.get("datetime")
                if not dt and row and row["captured_local"]:
                    dt = exif_mod.local_iso_to_exif(row["captured_local"])
                if dt:
                    exif_mod.inject_exif_into_jpeg(
                        dst, dt, src_tags.get("make"), src_tags.get("model"))
            # 古いビューア用プレビューは消す（次回表示時に遅延生成される）
            stale_view = safe_join(
                common.VIEW_DIR, Path(rel).with_suffix("").as_posix() + "_view.webp")
            try:
                if stale_view is not None and stale_view.is_file():
                    stale_view.unlink()
            except OSError:
                pass
            try:
                from . import backup
                backup.mark_dirty()
            except Exception:
                pass
            self.send_json({"ok": True, "path": rel})
        except Exception as e:  # noqa: BLE001
            self.send_json({"error": str(e)}, 500)
        finally:
            try:
                payload.close()
            except Exception:
                pass

    def api_edit_saveas(self) -> None:
        """編集結果を元画像と同じフォルダに別名保存する
        （multipart: path + name + file）。

        写真が対象。同フォルダに name（重複時は連番）で保存し DB 登録する。
        JPEG なら元画像の EXIF（撮影日時・メーカー・モデル）を引き継ぐため、
        並び順は撮影日時のまま（一番上に来ない）。
        "edit/<名>" が対象なら編集フォルダに保存する（DB 登録なし）。
        """
        from . import exif as exif_mod
        from . import ingest

        got, err = self._read_multipart_file()
        if got is None:
            self.send_json({"error": err}, 400)
            return
        fields, _filename, payload = got
        try:
            src_rel = (fields.get("path") or "").strip()
            name = ingest.sanitize_filename(fields.get("name") or _filename or "edit")
            if not src_rel:
                self.send_json({"error": "path required"}, 400)
                return
            if "/" in name or not name:
                self.send_json({"error": "bad name"}, 400)
                return
            data = payload.read()
            if src_rel.startswith("edit/"):
                src_name = src_rel[len("edit/"):]
                src_file = safe_join(common.EDIT_PHOTO_DIR, src_name)
                if src_file is None or "/" in src_name:
                    self.send_json({"error": "bad path"}, 400)
                    return
                base_dir = common.EDIT_PHOTO_DIR
                in_edit = True
                fallback_dt = None
            else:
                src_file = safe_join(common.PHOTO_DIR, src_rel)
                if src_file is None or not src_file.is_file():
                    self.send_json({"error": "photo not found"}, 404)
                    return
                base_dir = src_file.parent
                in_edit = False
                fallback_dt = None
                conn = common.get_db()
                srow = conn.execute(
                    "SELECT captured_local FROM photos WHERE path=?", (src_rel,)).fetchone()
                if srow and srow["captured_local"]:
                    fallback_dt = exif_mod.local_iso_to_exif(srow["captured_local"])
            tags = (exif_mod.edit_source_tags(src_file)
                    if src_file is not None and src_file.is_file()
                    else {"datetime": None, "make": None, "model": None})
            base_dir.mkdir(parents=True, exist_ok=True)
            dest = base_dir / name
            n = 1
            while dest.exists():
                dest = base_dir / f"{Path(name).stem}_{n}{Path(name).suffix}"
                n += 1
            dest.write_bytes(data)
            dt = tags.get("datetime") or fallback_dt
            if dt and data[:2] == b"\xff\xd8":
                exif_mod.inject_exif_into_jpeg(
                    dest, dt, tags.get("make"), tags.get("model"))
            if in_edit:
                try:
                    from . import backup
                    backup.mark_dirty()
                except Exception:
                    pass
                self.send_json({"ok": True, "path": f"edit/{dest.name}",
                                "filename": dest.name})
                return
            pid = ingest.finalize_upload(dest)
            try:
                from . import backup
                backup.mark_dirty()
            except Exception:
                pass
            self.send_json({"ok": True, "id": pid,
                            "path": dest.relative_to(common.PHOTO_DIR).as_posix(),
                            "filename": dest.name})
        except Exception as e:  # noqa: BLE001
            self.send_json({"error": str(e)}, 500)
        finally:
            try:
                payload.close()
            except Exception:
                pass

    def api_rename(self) -> None:
        """ファイル名を変更する（拡張子は維持・同一フォルダ内のみ）。

        POST /api/rename {"path": "2026/202609/20260911_/IMG_0001.jpg", "name": "新しい名前"}
        name は拡張子より前の部分。DB・サムネイルも追従する。
        "edit/<名>" は編集フォルダ内を対象にする。
        """
        from . import ingest

        body = self._read_json_body()
        if not isinstance(body, dict):
            self.send_json({"error": "bad json"}, 400)
            return
        rel = body.get("path") if isinstance(body.get("path"), str) else ""
        rel = rel.strip()
        name = body.get("name") if isinstance(body.get("name"), str) else ""
        if not rel or not name.strip():
            self.send_json({"error": "path and name required"}, 400)
            return
        # 拡張子より前の部分だけ使い、元の拡張子を維持する
        stem = Path(ingest.sanitize_filename(name.strip())).stem.strip(" .")
        if not stem or len(stem) > 120:
            self.send_json({"error": "bad name"}, 400)
            return
        if rel.startswith("edit/"):
            old_name = rel[len("edit/"):]
            if not old_name or "/" in old_name:
                self.send_json({"error": "bad path"}, 400)
                return
            src = safe_join(common.EDIT_PHOTO_DIR, old_name)
            if src is None or not src.is_file():
                self.send_json({"error": "photo not found"}, 404)
                return
            new_name = stem + src.suffix
            if new_name == old_name:
                self.send_json({"ok": True, "path": rel, "filename": old_name})
                return
            dst = safe_join(common.EDIT_PHOTO_DIR, new_name)
            if dst is None or "/" in new_name:
                self.send_json({"error": "bad name"}, 400)
                return
            if dst.exists():
                self.send_json({"error": "already exists"}, 409)
                return
            try:
                os.replace(src, dst)
                cache_old = common.THUMB_DIR / "edit" / (Path(old_name).stem + "_thumb.webp")
                cache_new = common.THUMB_DIR / "edit" / (Path(new_name).stem + "_thumb.webp")
                if cache_old.is_file() and not cache_new.exists():
                    os.replace(cache_old, cache_new)
            except OSError as e:
                self.send_json({"error": str(e)}, 500)
                return
            self.send_json({"ok": True, "path": f"edit/{new_name}", "filename": new_name})
            return
        src = safe_join(common.PHOTO_DIR, rel)
        if src is None or not src.is_file():
            self.send_json({"error": "photo not found"}, 404)
            return
        new_name = stem + src.suffix
        if new_name == src.name:
            self.send_json({"ok": True, "path": rel, "filename": src.name})
            return
        if "/" in new_name:
            self.send_json({"error": "bad name"}, 400)
            return
        dst = src.parent / new_name
        if dst.exists():
            self.send_json({"error": "already exists"}, 409)
            return
        conn = common.get_db()
        row = conn.execute("SELECT * FROM photos WHERE path=?", (rel,)).fetchone()
        if row is None:
            self.send_json({"error": "photo not found"}, 404)
            return
        new_rel = (Path(rel).parent / new_name).as_posix()
        new_thumb_rel = Path(new_rel).with_suffix("").as_posix() + "_thumb.webp"
        try:
            os.replace(src, dst)
            # サムネイルも追従（無ければ生成時に作り直される）
            if row["thumb_path"]:
                old_thumb = safe_join(common.THUMB_DIR, row["thumb_path"])
                new_thumb = safe_join(common.THUMB_DIR, new_thumb_rel)
                if (old_thumb is not None and new_thumb is not None
                        and old_thumb.is_file() and not new_thumb.exists()):
                    new_thumb.parent.mkdir(parents=True, exist_ok=True)
                    os.replace(old_thumb, new_thumb)
            # ビューア用プレビューも追従（無ければ表示時に作り直される）
            old_view_rel = Path(rel).with_suffix("").as_posix() + "_view.webp"
            new_view_rel = Path(new_rel).with_suffix("").as_posix() + "_view.webp"
            old_view = safe_join(common.VIEW_DIR, old_view_rel)
            new_view = safe_join(common.VIEW_DIR, new_view_rel)
            if (old_view is not None and new_view is not None
                    and old_view.is_file() and not new_view.exists()):
                new_view.parent.mkdir(parents=True, exist_ok=True)
                os.replace(old_view, new_view)
            conn.execute("UPDATE photos SET path=?, filename=?, thumb_path=? WHERE path=?",
                         (new_rel, new_name, new_thumb_rel, rel))
            conn.commit()
        except OSError as e:
            self.send_json({"error": str(e)}, 500)
            return
        try:
            from . import backup
            backup.mark_dirty()
        except Exception:
            pass
        self.send_json({"ok": True, "path": new_rel, "filename": new_name})

    def api_rotate(self) -> None:
        """元画像を90度回転して上書き保存する（EXIF維持・サムネイル更新）。

        POST /api/rotate {"path": "2026/202609/20260911_/IMG_0001.jpg",
                          "dir": "left" | "right"}
        left=反時計回り90度、right=時計回り90度。
        "edit/<名>" は編集フォルダ内を対象にする（DB なし・サムネイルキャッシュ削除）。
        動画は対象外。
        """
        from . import exif as exif_mod
        from . import ingest

        body = self._read_json_body()
        if not isinstance(body, dict):
            self.send_json({"error": "bad json"}, 400)
            return
        rel = body.get("path") if isinstance(body.get("path"), str) else ""
        rel = (rel or "").strip()
        direction = body.get("dir") if isinstance(body.get("dir"), str) else ""
        direction = (direction or "").strip().lower()
        if direction not in ("left", "right"):
            self.send_json({"error": "dir must be left or right"}, 400)
            return
        if not rel:
            self.send_json({"error": "path required"}, 400)
            return
        try:
            from PIL import Image, ImageOps
        except ImportError:
            self.send_json({"error": "Pillow not available"}, 500)
            return

        def rotate_file(src: Path) -> tuple[int | None, int | None]:
            """src を90度回転して同形式で上書き保存する。EXIF は維持する。

            戻り値は (width, height)。失敗時は例外を投げる。
            """
            suffix = src.suffix.lower()
            # 回転前に元画像の EXIF（撮影日時・メーカー・モデル）を読む。
            # Pillow の EXIF 引き継ぎが落ちた場合のフォールバック用（JPEG のみ）。
            try:
                src_tags = exif_mod.edit_source_tags(src)
            except Exception:
                src_tags = {"datetime": None, "make": None, "model": None}
            try:
                icc = None
                try:
                    with Image.open(src) as _probe:
                        icc = _probe.info.get("icc_profile")
                except Exception:
                    icc = None
            except Exception:
                icc = None
            with Image.open(src) as im:
                orig_format = (im.format or "").upper()
                # Exif Orientation を正規化してから回転する
                try:
                    im = ImageOps.exif_transpose(im)
                except Exception:
                    pass
                if im is None:
                    raise ValueError("exif_transpose failed")
                op = Image.ROTATE_90 if direction == "left" else Image.ROTATE_270
                # アニメGIFは全フレームを回転する
                is_animated_gif = (
                    suffix == ".gif"
                    and getattr(im, "format", "") == "GIF"
                    and getattr(im, "n_frames", 1) > 1
                )
                if is_animated_gif:
                    frames = []
                    try:
                        n = im.n_frames
                    except Exception:
                        n = 1
                    for i in range(n):
                        try:
                            im.seek(i)
                        except Exception:
                            break
                        f = ImageOps.exif_transpose(im.copy())
                        frames.append(f.transpose(op))
                    if not frames:
                        raise ValueError("gif decode failed")
                    tmp = src.with_name(src.name + ".rotate-tmp")
                    first, rest = frames[0], frames[1:]
                    first.save(tmp, format="GIF", save_all=True,
                               append_images=rest, loop=0)
                    os.replace(tmp, src)
                    return frames[0].size
                im = im.transpose(op)
                # EXIF は Orientation=1（正位置）に正規化して引き継ぐ
                exif_bytes = None
                try:
                    ex = im.getexif()
                    if ex is not None and len(ex) > 0:
                        try:
                            ex[0x0112] = 1
                        except Exception:
                            pass
                        exif_bytes = ex.tobytes()
                except Exception:
                    exif_bytes = None
                if exif_bytes is None:
                    try:
                        exif_bytes = im.info.get("exif")
                    except Exception:
                        exif_bytes = None
                tmp = src.with_name(src.name + ".rotate-tmp")
                try:
                    if suffix in (".jpg", ".jpeg"):
                        if im.mode in ("RGBA", "LA", "PA", "P"):
                            im = im.convert("RGB")
                        kw: dict = {"format": "JPEG", "quality": 95,
                                    "subsampling": 1}
                        if exif_bytes:
                            kw["exif"] = exif_bytes
                        if icc:
                            kw["icc_profile"] = icc
                        im.save(tmp, **kw)
                    elif suffix == ".png":
                        kw = {"format": "PNG", "optimize": True}
                        if exif_bytes:
                            kw["exif"] = exif_bytes
                        if icc:
                            kw["icc_profile"] = icc
                        im.save(tmp, **kw)
                    elif suffix == ".webp":
                        kw = {"format": "WEBP", "quality": 92, "method": 4}
                        if exif_bytes:
                            kw["exif"] = exif_bytes
                        if icc:
                            kw["icc_profile"] = icc
                        im.save(tmp, **kw)
                    elif suffix in (".tif", ".tiff"):
                        kw = {"format": "TIFF"}
                        if exif_bytes:
                            kw["exif"] = exif_bytes
                        im.save(tmp, **kw)
                    elif suffix == ".bmp":
                        im.save(tmp, format="BMP")
                    elif suffix == ".gif":
                        im.save(tmp, format="GIF")
                    else:
                        # heic/heif/avif 等: 元フォーマットで保存を試みる
                        fmt = orig_format or suffix.lstrip(".").upper()
                        kw = {"format": fmt}
                        if exif_bytes and fmt in ("JPEG", "PNG", "WEBP", "TIFF"):
                            kw["exif"] = exif_bytes
                        if fmt == "JPEG":
                            if im.mode in ("RGBA", "LA", "PA", "P"):
                                im = im.convert("RGB")
                            kw.setdefault("quality", 95)
                        im.save(tmp, **kw)
                except Exception:
                    try:
                        if tmp.is_file():
                            tmp.unlink()
                    except OSError:
                        pass
                    raise
                os.replace(tmp, src)
                # JPEG は Pillow の EXIF 引き継ぎが落ちることがあるため、
                # 撮影日時が消えていたら日時・メーカー・モデルだけ補う
                if suffix in (".jpg", ".jpeg"):
                    try:
                        head = src.read_bytes()[:2]
                    except OSError:
                        head = b""
                    if head == b"\xff\xd8" and src_tags.get("datetime"):
                        try:
                            cur = exif_mod.edit_source_tags(src)
                        except Exception:
                            cur = {"datetime": None}
                        if not cur.get("datetime"):
                            exif_mod.inject_exif_into_jpeg(
                                src, src_tags["datetime"],
                                src_tags.get("make"), src_tags.get("model"))
                return im.size[0], im.size[1]

        if rel.startswith("edit/"):
            name = rel[len("edit/"):]
            dst = safe_join(common.EDIT_PHOTO_DIR, name)
            if dst is None or "/" in name or not dst.is_file():
                self.send_json({"error": "photo not found"}, 404)
                return
            if dst.suffix.lower() in common.VIDEO_EXTS:
                self.send_json({"error": "video not supported"}, 400)
                return
            try:
                rotate_file(dst)
            except Exception as e:  # noqa: BLE001
                self.send_json({"error": f"rotate failed: {e}"}, 500)
                return
            cache = common.THUMB_DIR / "edit" / (Path(name).stem + "_thumb.webp")
            try:
                if cache.is_file():
                    cache.unlink()
            except OSError:
                pass
            try:
                from . import backup
                backup.mark_dirty()
            except Exception:
                pass
            try:
                v = int(dst.stat().st_mtime)
            except OSError:
                v = int(time.time())
            self.send_json({
                "ok": True, "path": rel, "filename": dst.name,
                "thumb": f"/editthumb/{name}?v={v}",
                "original": f"/editphoto/{name}?v={v}",
            })
            return
        dst = safe_join(common.PHOTO_DIR, rel)
        if dst is None or not dst.is_file():
            self.send_json({"error": "photo not found"}, 404)
            return
        if dst.suffix.lower() in common.VIDEO_EXTS:
            self.send_json({"error": "video not supported"}, 400)
            return
        try:
            rotate_file(dst)
        except Exception as e:  # noqa: BLE001
            self.send_json({"error": f"rotate failed: {e}"}, 500)
            return
        conn = common.get_db()
        try:
            size = exif_mod.image_size(dst)
        except Exception:
            size = None
        try:
            st = dst.stat()
        except OSError as e:
            self.send_json({"error": str(e)}, 500)
            return
        conn.execute(
            """UPDATE photos SET size=?, mtime=?, hash=?,
                      width=?, height=?, thumb_done=0 WHERE path=?""",
            (st.st_size, st.st_mtime, ingest.file_hash_of(dst),
             size[0] if size else None, size[1] if size else None, rel),
        )
        conn.commit()
        row = conn.execute("SELECT * FROM photos WHERE path=?", (rel,)).fetchone()
        if row and row["thumb_path"]:
            old = safe_join(common.THUMB_DIR, row["thumb_path"])
            try:
                if old is not None and old.is_file():
                    old.unlink()
            except OSError:
                pass
        if row:
            ingest.make_thumbnail(row)
        stale_view = safe_join(
            common.VIEW_DIR, Path(rel).with_suffix("").as_posix() + "_view.webp")
        try:
            if stale_view is not None and stale_view.is_file():
                stale_view.unlink()
        except OSError:
            pass
        try:
            from . import backup
            backup.mark_dirty()
        except Exception:
            pass
        v = int(st.st_mtime)
        self.send_json({
            "ok": True, "path": rel, "filename": dst.name,
            "width": size[0] if size else None,
            "height": size[1] if size else None,
            "size": st.st_size, "mtime": v,
            "thumb": f"/thumb/{Path(rel).with_suffix('').as_posix()}_thumb.webp?v={v}",
            "view": f"/view/{ingest.view_rel_path(rel)}?v={v}",
            "original": f"/photo/{rel}?v={v}",
        })

    def _read_json_body(self, max_len: int = 1024 * 1024):
        """JSON ボディを読む。失敗時は None。"""
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = 0
        if length <= 0 or length > max_len:
            return None
        try:
            return json.loads(self.rfile.read(length).decode("utf-8"))
        except Exception:
            return None

    def api_backup_config(self) -> None:
        """バックアップ設定を返す（パスワードはマスク）。"""
        from . import backup

        cfg = backup.load_config()
        ssh = dict(cfg.get("ssh") or {})
        ssh["password"] = "****" if ssh.get("password") else ""
        self.send_json({
            "source": cfg.get("source"), "target": cfg.get("target"),
            "ssh": ssh, "watch": cfg.get("watch"),
            "fixedOptions": backup.FIXED_OPTS,
            "exclude": backup.EXCLUDE_UPLOAD_TMP,
            "intervals": backup.WATCH_INTERVALS,
            "rsyncAvailable": backup.rsync_available(),
            "watching": backup.is_watching(),
            "lastRun": cfg.get("lastRun"),
        })

    def api_backup_save(self) -> None:
        """バックアップ設定を保存する。"""
        from . import backup

        body = self._read_json_body()
        if not isinstance(body, dict):
            self.send_json({"error": "bad json"}, 400)
            return
        cfg = backup.load_config()
        for k in ("source", "target"):
            if k in body and isinstance(body[k], str):
                cfg[k] = body[k].strip()
        if isinstance(body.get("ssh"), dict):
            for k in ("enabled", "host", "user", "port", "key", "password"):
                if k in body["ssh"]:
                    v = body["ssh"][k]
                    cfg["ssh"][k] = bool(v) if k == "enabled" else (str(v) if v is not None else "")
            # "****" のままなら既存パスワードを維持する
            if body["ssh"].get("password") in (None, "", "****"):
                cfg["ssh"]["password"] = backup.load_config()["ssh"].get("password", "")
        if isinstance(body.get("watch"), dict):
            if "enabled" in body["watch"]:
                cfg["watch"]["enabled"] = bool(body["watch"]["enabled"])
            if "mode" in body["watch"]:
                cfg["watch"]["mode"] = str(body["watch"]["mode"])
            if "intervalSec" in body["watch"]:
                try:
                    cfg["watch"]["intervalSec"] = int(body["watch"]["intervalSec"])
                except (TypeError, ValueError):
                    pass
            if "times" in body["watch"]:
                cfg["watch"]["times"] = body["watch"]["times"]
            if "days" in body["watch"]:
                cfg["watch"]["days"] = body["watch"]["days"]
            cfg["watch"] = backup.normalize_watch(cfg["watch"])
        err = backup.validate_config(cfg)
        if err:
            self.send_json({"error": err}, 400)
            return
        backup.save_config(cfg)
        backup.set_watch(bool(cfg["watch"].get("enabled")))
        self.send_json({"ok": True})

    def api_backup_run(self) -> None:
        """バックアップを今すぐ実行する（バックグラウンド）。"""
        from . import backup

        r = backup.run_async(detail="manual")
        if r.get("alreadyRunning"):
            self.send_json({"error": "already running"}, 409)
            return
        self.send_json({"ok": True, "started": True})

    def api_backup_status(self) -> None:
        """バックアップの状態・前回結果を返す。"""
        from . import backup

        st = backup.status()
        st["watching"] = backup.is_watching()
        self.send_json(st)

    def api_backup_watch(self) -> None:
        """監視の開始・停止。"""
        from . import backup

        body = self._read_json_body() or {}
        enabled = bool(body.get("enabled"))
        cfg = backup.load_config()
        cfg["watch"]["enabled"] = enabled
        backup.save_config(cfg)
        r = backup.set_watch(enabled)
        self.send_json({"ok": True, "watching": r.get("watching", False)})

    def _backup_ssh_from_body(self, body: dict) -> dict:
        """リクエストボディ→SSH設定dict。無ければ保存済み設定を使う。"""
        from . import backup

        if isinstance(body.get("ssh"), dict):
            ssh = dict(backup.load_config().get("ssh") or {})
            for k in ("enabled", "host", "user", "port", "key", "password"):
                if k in body["ssh"]:
                    v = body["ssh"][k]
                    ssh[k] = bool(v) if k == "enabled" else (str(v) if v is not None else "")
            if body["ssh"].get("password") in (None, "", "****"):
                ssh["password"] = backup.load_config()["ssh"].get("password", "")
            return ssh
        return dict(backup.load_config().get("ssh") or {})

    def api_backup_ssh_test(self) -> None:
        """SSH接続だけ確認する（保存不要。接続エラー切り分け用）。"""
        from . import backup

        body = self._read_json_body() or {}
        ssh = self._backup_ssh_from_body(body)
        r = backup.test_ssh_connection(ssh)
        if r.get("ok"):
            self.send_json({"ok": True, "message": r.get("message", "SSH接続OK"),
                            "diagnostics": r.get("diagnostics")})
        else:
            self.send_json({"ok": False, "error": r.get("error", "ssh failed"),
                            "diagnostics": r.get("diagnostics")}, 400)

    def api_backup_target_check(self) -> None:
        """ターゲットフォルダの確認。無い場合は作成する（mkdir -p）。"""
        from . import backup

        body = self._read_json_body() or {}
        target = body.get("target")
        if not isinstance(target, str) or not target:
            target = backup.load_config().get("target") or ""
        ssh = self._backup_ssh_from_body(body)
        r = backup.check_target(target, ssh, create=True)
        if r.get("ok"):
            self.send_json({"ok": True, "created": bool(r.get("created")),
                            "message": r.get("message", "OK")})
        else:
            self.send_json({"ok": False, "error": r.get("error", "target check failed")}, 400)

    def _write_chunk(self, data: bytes) -> None:
        if data:
            self.wfile.write(f"{len(data):X}\r\n".encode() + data + b"\r\n")
        else:
            self.wfile.write(b"0\r\n\r\n")
        self.wfile.flush()

    @staticmethod
    def _zip_chunks(base: Path, prefix: str):
        """base 以下の対応拡張子ファイルを zip 形式で逐次 yield する。"""
        files = sorted(
            (p for p in base.rglob("*")
             if p.is_file() and p.suffix.lower() in common.SUPPORTED_EXTS),
            key=lambda p: str(p),
        )
        with tempfile.SpooledTemporaryFile(max_size=16 * 1024 * 1024) as buf:
            # ZipFile はシーク可能な出力を要求するため、スプールに書いて
            # 順次読み出す方式でストリーミングする
            with zipfile.ZipFile(file=buf, mode="w", compression=zipfile.ZIP_STORED) as zf:
                last_flush = 0
                for p in files:
                    rel = p.relative_to(base).as_posix()
                    arcname = f"{prefix}/{rel}" if prefix else rel
                    with p.open("rb") as src, zf.open(arcname, "w") as dst:
                        shutil.copyfileobj(src, dst, length=1024 * 1024)
                    pos = buf.tell()
                    if pos - last_flush >= 512 * 1024:
                        buf.seek(last_flush)
                        yield buf.read(pos - last_flush)
                        last_flush = pos
                # 中央ディレクトリを書き込むため close が返るまで flush しない
            buf.seek(last_flush)
            rest = buf.read()
            if rest:
                yield rest

    # ------------------------------------------------------------------
    # HTML
    # ------------------------------------------------------------------
    def send_html(self) -> None:
        body = PAGE.replace("{__VERSION__}", common.VERSION).encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)


PAGE = r"""<!doctype html>
<html lang="ja">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<link rel="icon" type="image/png" href="/favicon.ico">
<link rel="icon" type="image/png" sizes="32x32" href="/icon/selfphotofav.png">
<link rel="apple-touch-icon" href="/apple-touch-icon.png">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<meta name="apple-mobile-web-app-title" content="selfphoto">
<title>selfphoto</title>
<style>
:root {
  --bg: #0e0f11; --fg: #e8eaed; --muted: #9aa0a6; --accent: #4c8dff;
  --card: #17181b; --chip: #26282d; --sidebar: #131417; --line: #232529;
}
* { box-sizing: border-box; }
html, body { margin: 0; height: 100%; }
body {
  background: var(--bg); color: var(--fg);
  font-family: system-ui, -apple-system, "Segoe UI", Roboto, "Noto Sans JP", sans-serif;
}
/* ビューア・編集画面は全画面オーバーレイのため、開いている間は背面の
   スクロールを止める（右端のネイティブスクロールバーも消える）。 */
body.modal-open { overflow: hidden; }
/* ---------------- sidebar ---------------- */
#sidebar {
  position: fixed; top: 0; bottom: 0; left: 0; width: 220px; z-index: 30;
  background: var(--sidebar); border-right: 1px solid var(--line);
  display: flex; flex-direction: column; padding: 14px 10px;
}
#sidebar .logo {
  display: flex; align-items: center; gap: 7px;
  font-weight: 700; font-size: 17px; padding: 6px 10px 14px; letter-spacing: .3px;
}
#sidebar .logo .logo-icon {
  width: 20px; height: 20px; border-radius: 5px; object-fit: cover;
  flex: none;
}
#sidebar .logo .ver {
  color: var(--muted); font-weight: 400; font-size: 11px; letter-spacing: 0;
}
#sidebar .foot #restart-btn, #sidebar .foot #update-btn {
  width: 100%; display: flex; align-items: center; justify-content: flex-start; gap: 6px;
  background: none; border: 1px solid var(--line); color: var(--muted);
  border-radius: 8px; padding: 7px 10px; font-size: 12px; cursor: pointer;
}
#sidebar .foot #restart-btn:hover, #sidebar .foot #update-btn:hover { color: var(--fg); border-color: #3a3d44; background: #1d1f24; }
#sidebar .foot #update-btn { margin-top: 6px; }
#restart-ov {
  position: fixed; inset: 0; z-index: 200; display: none;
  background: rgba(0,0,0,.72); align-items: center; justify-content: center;
}
#restart-ov.on { display: flex; }
#restart-ov .ro-inner {
  background: var(--card); border: 1px solid var(--line); color: var(--fg);
  padding: 16px 26px; border-radius: 12px; font-size: 14px; font-weight: 600;
}
#sidebar nav { display: flex; flex-direction: column; gap: 2px; }
#sidebar nav button {
  display: flex; align-items: center; gap: 10px;
  background: none; border: 0; color: var(--fg);
  font-size: 14px; text-align: left; padding: 10px 12px;
  border-radius: 10px; cursor: pointer;
}
#sidebar nav button:hover { background: #1d1f24; }
#sidebar nav button.active { background: #232833; color: #fff; }
#sidebar nav .ico {
  width: 20px; display: flex; justify-content: center; align-items: center; opacity: .9;
}
#sidebar nav .ico svg { display: block; }
#sidebar .foot {
  margin-top: auto; padding: 10px 12px; color: var(--muted); font-size: 11px;
}
#side-backup {
  display: flex; flex-direction: column; gap: 2px;
  padding: 0 2px 8px; font-size: 11px; color: var(--muted); line-height: 1.5;
  cursor: pointer;
}
#side-backup .sb-row {
  display: flex; align-items: center; gap: 5px;
  white-space: nowrap; overflow: hidden;
}
#side-backup .dot { width: 7px; height: 7px; border-radius: 50%; background: #555; flex: none; }
#side-backup .dot.on { background: #7ee2a8; }
#side-backup .dot.run { background: var(--accent); }
#side-backup .dot.ok { background: #7ee2a8; }
#side-backup .dot.ng { background: #ff9a9a; }

/* ---------------- main (header-less timeline) ---------------- */
main { padding: 0 8px 80px 228px; }
/* ---------------- sidebar extras ---------------- */
#sidebar nav .nav-count {
  margin-left: auto; color: var(--muted); font-size: 11px; font-weight: 400; flex: none;
}
#sidebar nav button.on .nav-count { color: #fff; }
#search-box {
  display: none; margin: 2px 6px; width: calc(100% - 12px);
  background: var(--chip); border: 1px solid #33363c; border-radius: 8px;
  padding: 7px 10px; color: var(--fg); font-size: 13px; outline: none;
}
#search-box:focus { border-color: var(--accent); }
/* ---------------- backup ---------------- */
#backup-form { max-width: 640px; padding: 8px 4px 40px; display: flex; flex-direction: column; gap: 10px; }
#backup-form h2 { font-size: 17px; margin: 8px 0 0; }
#backup-form h3 { font-size: 14px; margin: 12px 0 0; }
#backup-form .bk-desc { color: var(--muted); font-size: 12px; margin: 0; }
#backup-form code { background: var(--chip); padding: 1px 6px; border-radius: 5px; font-size: 12px; }
#backup-form label { display: flex; align-items: center; gap: 8px; font-size: 13px; }
#backup-form input[type="text"], #backup-form input[type="password"], #backup-form select {
  flex: 1; background: var(--chip); border: 1px solid #33363c; border-radius: 8px;
  padding: 7px 10px; color: var(--fg); font-size: 13px; outline: none; min-width: 0;
}
#backup-form fieldset { border: 1px solid var(--line); border-radius: 10px; padding: 10px 12px; display: flex; flex-direction: column; gap: 8px; }
#backup-form legend { font-size: 13px; padding: 0 6px; }
#backup-form .bk-check { font-size: 13px; }
#bk-ssh-fields { display: flex; flex-direction: column; gap: 8px; }
#backup-form .bk-row { display: flex; gap: 8px; align-items: center; font-size: 13px; flex-wrap: wrap; }
#backup-form .bk-row button {
  background: var(--chip); color: var(--fg); border: 0; border-radius: 8px;
  padding: 8px 16px; font-size: 13px; cursor: pointer;
}
#backup-form .bk-row button:hover { background: #33363c; }
#backup-form .bk-msg { font-size: 12px; }
#backup-form .bk-msg.ok { color: #7ee2a8; }
#backup-form .bk-msg.ng { color: #ff9a9a; }
#bk-status { font-size: 13px; color: var(--fg); background: var(--card); border: 1px solid var(--line); border-radius: 8px; padding: 8px 12px; }
#backup-form input[type="time"] {
  background: var(--chip); border: 1px solid #33363c; border-radius: 8px;
  padding: 6px 8px; color: var(--fg); font-size: 13px; outline: none;
}
#bk-days { display: flex; gap: 6px; flex-wrap: wrap; }
#bk-days .day-btn {
  min-width: 34px; text-align: center; padding: 6px 8px; font-size: 13px;
  border: 1px solid #33363c; border-radius: 8px; cursor: pointer;
  background: var(--chip); color: var(--muted); user-select: none;
}
#bk-days .day-btn.on { background: var(--accent); border-color: var(--accent); color: #fff; }
#bk-times { display: flex; flex-direction: column; gap: 6px; }
#bk-times .bk-time-row { display: flex; gap: 6px; align-items: center; }
#bk-times .bk-time-row button {
  padding: 6px 10px; font-size: 12px;
}
#bk-log {
  background: #0a0b0d; border: 1px solid var(--line); border-radius: 8px;
  padding: 10px 12px; font-size: 11px; color: var(--muted);
  max-height: 320px; overflow: auto; white-space: pre-wrap; margin: 0;
}

/* ---------------- timeline ---------------- */
.month-head {
  position: sticky; top: 0; z-index: 9;
  padding: 14px 10px 6px; font-weight: 700; font-size: 15px;
  background: linear-gradient(var(--bg), rgba(14,15,17,.85));
}
.grid {
  display: flex; flex-wrap: wrap; gap: 3px;
}
.cell {
  position: relative; height: 180px; flex: 0 0 auto; overflow: hidden;
  background: var(--card); border-radius: 4px; cursor: pointer;
}
@media (max-width: 640px) { .cell { height: 110px; } }
.cell img {
  width: 100%; height: 100%; object-fit: cover; display: block;
  opacity: 0; transition: opacity .25s;
}
.cell img.loaded { opacity: 1; }
.cell .badge {
  position: absolute; right: 5px; bottom: 5px;
  background: rgba(0,0,0,.55); border-radius: 4px; padding: 1px 5px;
  font-size: 10px; color: #fff; pointer-events: none;
}

/* ---------------- year/month scrubber (right edge) ---------------- */
#scrubber {
  position: fixed; top: 60px; bottom: 40px; right: 2px; width: 46px; z-index: 20;
  display: flex; flex-direction: column; justify-content: space-between;
  align-items: flex-end; padding: 4px 2px;
}
#scrubber .yr {
  color: var(--muted); font-size: 11px; font-weight: 600;
  background: rgba(14,15,17,.5); padding: 2px 6px; border-radius: 6px 0 0 6px;
  cursor: pointer; user-select: none;
}
#scrubber .yr.now { color: #fff; background: var(--accent); }
#scrubber .mo {
  display: flex; align-items: center; gap: 6px; cursor: pointer;
  color: var(--muted); font-size: 10px;
}
#scrubber .mo .bar { width: 22px; height: 3px; border-radius: 2px; background: #3a3d44; }
#scrubber .mo:hover .bar { background: var(--accent); }
#scrubber .mo.hasyear .bar { width: 34px; background: #565b64; }
#scrubber .mo span.lbl {
  display: none; background: var(--chip); color: var(--fg);
  padding: 2px 7px; border-radius: 6px; white-space: nowrap;
}
#scrubber .mo:hover span.lbl { display: inline; }
#now-viewing {
  position: fixed; right: 56px; top: 50%; transform: translateY(-50%);
  z-index: 21; background: rgba(23,24,27,.95); border: 1px solid var(--line);
  color: var(--fg); padding: 10px 14px; border-radius: 10px;
  font-size: 13px; font-weight: 600; display: none; pointer-events: none;
  box-shadow: 0 4px 18px rgba(0,0,0,.5);
}
#now-viewing .nv-sub { color: var(--muted); font-weight: 400; font-size: 11px; }
#lightbox {
  position: fixed; inset: 0; z-index: 50; display: none;
  background: rgba(0,0,0,.96);
  align-items: center; justify-content: center;
}
#lightbox.open { display: flex; }
#lb-content {
  position: absolute; top: 0; right: 0; bottom: 0; left: 96px; padding-top: 44px;
  display: flex; overflow: auto;
}
#lb-content img, #lb-content video { margin: auto; flex: none; }
#lightbox img.fit, #lightbox video {
  max-width: calc(100vw - 96px - 16px); max-height: calc(100vh - 52px);
  object-fit: contain;
}
#lightbox img.zoomed { max-width: none; max-height: none; cursor: grab; }
/* ---------------- viewer left rail ---------------- */
#lb-rail {
  position: fixed; left: 0; top: 0; bottom: 0; z-index: 6;
  width: 96px; display: flex; flex-direction: column; gap: 6px;
  padding: 12px 8px; background: rgba(10,10,12,.88); border-right: 1px solid #222;
}
#lb-rail .rail-spacer { flex: 1; }
#lb-titlebar {
  position: fixed; top: 0; left: 96px; right: 0; z-index: 5;
  padding: 10px 14px; color: #ddd; font-size: 13px; pointer-events: none;
  background: linear-gradient(rgba(0,0,0,.6), transparent);
  white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
}
#lightbox button {
  background: var(--chip); color: var(--fg); border: 0; border-radius: 8px;
  padding: 8px 12px; font-size: 14px; cursor: pointer;
}
#lb-rail button { width: 100%; padding: 10px 4px; font-size: 13px; flex: none; }
#lb-rail button:hover { background: #33363c; }
#lightbox button:disabled { opacity: .4; cursor: default; }
#lb-zoom-label { color: var(--muted); font-size: 12px; text-align: center; }
#lb-del { background: #5a2326; }
#lb-del:hover { background: #752e33; }
#lightbox .nav {
  position: fixed; top: 50%; transform: translateY(-50%); z-index: 5;
  font-size: 26px; padding: 14px 16px; opacity: .75;
}
#prev { left: 104px; } #next { right: 8px; }
#loading { text-align: center; color: var(--muted); padding: 24px; }
/* ---------------- editor ---------------- */
#editor {
  position: fixed; inset: 0; z-index: 60; display: none;
  background: rgba(0,0,0,.97); flex-direction: column;
}
#editor.open { display: flex; }
#editor .ed-head { padding: 10px 14px; color: #ddd; font-size: 13px; flex: none; }
#editor .ed-main { flex: 1; display: flex; gap: 10px; padding: 0 14px; min-height: 0; }
#editor .ed-canvas-wrap {
  flex: 1; position: relative; display: flex; align-items: center; justify-content: center;
  background: #000; overflow: auto; min-width: 0;
}
/* 表示ズーム時（fit以外）はブロック配置に切り替える。
   flex の中央寄せのままだとはみ出し分の上・左へスクロールできないため。 */
#editor .ed-canvas-wrap.zoomed { display: block; }
#editor .ed-canvas-wrap.zoomed #ed-canvas { display: block; margin: 0 auto; max-width: none; max-height: none; }
#ed-canvas { max-width: 100%; max-height: 100%; touch-action: none; cursor: crosshair; }
#ed-cropbox {
  position: absolute; display: none; z-index: 2; pointer-events: none;
  border: 2px dashed var(--accent); background: rgba(76,141,255,.12);
}
#editor .ed-side {
  width: 190px; flex: none; display: flex; flex-direction: column; gap: 6px; overflow-y: auto;
  order: -1;
}
#editor .ed-side > button {
  background: var(--chip); color: var(--fg); border: 0; border-radius: 8px;
  padding: 12px 8px; font-size: 14px; cursor: pointer;
}
#editor .ed-side > button.on { background: var(--accent); color: #fff; }
#editor .ed-side > button:disabled { opacity: .4; cursor: default; }
#ed-undo { margin-top: auto; }
#editor .ed-panel {
  display: none; background: var(--card); border: 1px solid var(--line);
  border-radius: 8px; padding: 10px; font-size: 12px; color: var(--fg);
  flex-direction: column; gap: 8px;
}
#editor .ed-panel.on { display: flex; }
#editor .ed-panel label { display: flex; align-items: center; gap: 6px; }
#editor .ed-panel input[type="number"] {
  width: 90px; background: #0e0f11; color: var(--fg);
  border: 1px solid var(--line); border-radius: 6px; padding: 5px 6px; font-size: 13px;
}
#editor .ed-panel input[type="range"] { flex: 1; }
#editor .ed-row { display: flex; gap: 6px; flex-wrap: wrap; }
#editor .ed-row button, #editor .ed-panel > button {
  background: var(--chip); color: var(--fg); border: 0; border-radius: 6px;
  padding: 6px 10px; font-size: 12px; cursor: pointer;
}
#editor .ed-row button:hover { background: #33363c; }
#editor .ed-hint { color: var(--muted); font-size: 11px; }
#editor .ed-savebar {
  flex: none; display: flex; gap: 8px; justify-content: center; padding: 12px;
}
#editor .ed-savebar button {
  background: var(--chip); color: var(--fg); border: 0; border-radius: 8px;
  padding: 9px 16px; font-size: 13px; cursor: pointer;
}
#editor .ed-savebar button:hover { background: #33363c; }
#ed-overwrite { background: #1d3a24 !important; }
#ed-tolibrary { background: #1d2f4a !important; }
/* ---------------- sidebar selection buttons ---------------- */
#sidebar nav button.on { background: var(--accent); color: #fff; }
#nav-download, #nav-delete { display: none; }
#nav-delete .ico { color: #ff9a9a; }
/* ---------------- selection mode ---------------- */
.cell { position: relative; }
.cell .sel-box {
  position: absolute; top: 5px; left: 5px; z-index: 5;
  display: none; align-items: center; justify-content: center;
  width: 22px; height: 22px; border-radius: 6px;
  background: rgba(0,0,0,.55); color: #fff; font-size: 15px; line-height: 1;
  border: 1.5px solid rgba(255,255,255,.75); cursor: pointer; user-select: none;
}
body.selecting .cell .sel-box { display: flex; }
.cell.selected .sel-box { background: var(--accent); border-color: var(--accent); }
.cell.selected img { outline: 2px solid var(--accent); outline-offset: -2px; }
.month-head .sel-box, .day-head .sel-box {
  position: absolute; top: 50%; left: 10px; transform: translateY(-50%);
  margin-left: 0; margin-top: 0;
  display: none; align-items: center; justify-content: center;
  width: 20px; height: 20px; border-radius: 6px;
  background: rgba(0,0,0,.45); color: #fff; font-size: 13px; line-height: 1;
  border: 1.5px solid rgba(255,255,255,.7); cursor: pointer; user-select: none;
}
body.selecting .month-head .sel-box, body.selecting .day-head .sel-box { display: flex; }
.month-head .sel-box { position: static; transform: none; margin-right: 8px; }
.day-head { position: relative; }
.month-head.on, .day-head.on { color: var(--accent); }
/* ---------------- thumbnail context menu ---------------- */
#ctx-menu {
  position: fixed; z-index: 120; display: none; min-width: 160px;
  background: var(--card); border: 1px solid var(--line); border-radius: 10px;
  box-shadow: 0 8px 30px rgba(0,0,0,.55); padding: 4px; overflow: hidden;
}
#ctx-menu.on { display: block; }
#ctx-menu button {
  display: flex; width: 100%; align-items: center; gap: 8px;
  background: none; border: 0; color: var(--fg);
  font-size: 13px; text-align: left; padding: 9px 12px;
  border-radius: 7px; cursor: pointer; white-space: nowrap;
}
#ctx-menu button:hover { background: #232833; }
#ctx-menu button.disabled { opacity: .4; cursor: default; }
#ctx-menu button.disabled:hover { background: none; }
#dropzone {
  position: fixed; inset: 0; z-index: 90; display: none;
  background: rgba(20,120,255,.18); backdrop-filter: blur(2px);
  border: 3px dashed var(--accent);
  align-items: center; justify-content: center;
}
#dropzone.on { display: flex; }
#dropzone .dz-inner {
  font-size: 22px; font-weight: 700; color: #fff;
  background: rgba(0,0,0,.6); padding: 18px 28px; border-radius: 12px;
}
#up-bar {
  position: fixed; left: 220px; right: 0; bottom: 0; z-index: 95;
  display: none; padding: 10px 16px 14px;
  background: rgba(14,15,17,.95); border-top: 1px solid #222;
}
#up-bar.on { display: block; }
#up-label { font-size: 12px; color: var(--muted); margin-bottom: 6px; }
#up-track { height: 6px; background: #26282d; border-radius: 3px; overflow: hidden; }
#up-fill { height: 100%; width: 0; background: var(--accent); transition: width .2s; }
/* ---------------- upload manager (Immich 風の逐次表示) ---------------- */
#up-manager {
  position: fixed; right: 12px; bottom: 12px; z-index: 96;
  width: 380px; max-width: calc(100vw - 24px);
  background: var(--card); border: 1px solid var(--line); border-radius: 12px;
  box-shadow: 0 8px 30px rgba(0,0,0,.55);
  display: none; overflow: hidden;
}
#up-manager.on { display: block; }
#up-manager .upm-head {
  display: flex; align-items: center; gap: 8px;
  padding: 10px 12px; font-size: 13px; font-weight: 700;
  border-bottom: 1px solid var(--line); cursor: pointer; user-select: none;
}
#up-manager .upm-head .upm-count { color: var(--muted); font-weight: 400; font-size: 12px; }
#up-manager .upm-head-btns { margin-left: auto; display: flex; gap: 4px; }
#up-manager .upm-head-btns button {
  background: none; border: 0; color: var(--muted); font-size: 15px;
  cursor: pointer; padding: 2px 8px; border-radius: 6px; line-height: 1.2;
}
#up-manager .upm-head-btns button:hover { background: var(--chip); color: var(--fg); }
#up-manager .upm-overall { padding: 10px 12px 6px; }
#up-manager .upm-track { height: 6px; background: #26282d; border-radius: 3px; overflow: hidden; }
#up-manager .upm-track > div { height: 100%; width: 0; background: var(--accent); transition: width .2s; }
#up-manager .upm-track.err > div { background: #e06868; }
#up-manager .upm-track.done > div { background: #7ee2a8; }
#upm-summary { font-size: 12px; color: var(--muted); margin-top: 6px; line-height: 1.5; }
#upm-list { max-height: 42vh; overflow-y: auto; padding: 2px 0 4px; }
#up-manager.min #upm-list, #up-manager.min .upm-foot { display: none; }
.upm-item { display: flex; gap: 10px; align-items: center; padding: 7px 12px; font-size: 12px; }
.upm-item + .upm-item { border-top: 1px solid var(--line); }
.upm-item img.th {
  width: 40px; height: 40px; object-fit: cover; border-radius: 6px;
  background: #26282d; flex: none;
}
.upm-item .upm-info { flex: 1; min-width: 0; }
.upm-item .upm-name {
  white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
  font-size: 12px; color: var(--fg);
}
.upm-item .upm-sub { color: var(--muted); font-size: 11px; margin-top: 2px; }
.upm-item .upm-bar { height: 4px; background: #26282d; border-radius: 2px; overflow: hidden; margin-top: 4px; }
.upm-item .upm-bar > div { height: 100%; width: 0; background: var(--accent); transition: width .15s; }
.upm-item[data-st="done"] .upm-bar > div { background: #7ee2a8; }
.upm-item[data-st="skipped"] .upm-bar > div { background: #9aa0a6; }
.upm-item[data-st="error"] .upm-bar > div, .upm-item[data-st="cancelled"] .upm-bar > div { background: #e06868; }
.upm-item .upm-st { flex: none; font-size: 11px; color: var(--muted); min-width: 64px; text-align: right; }
.upm-item[data-st="done"] .upm-st { color: #7ee2a8; }
.upm-item[data-st="skipped"] .upm-st { color: #9aa0a6; }
.upm-item[data-st="error"] .upm-st { color: #ff9a9a; }
.upm-item[data-st="uploading"] .upm-st, .upm-item[data-st="processing"] .upm-st { color: var(--fg); }
#up-manager .upm-foot {
  display: flex; gap: 8px; justify-content: flex-end;
  padding: 8px 12px 12px;
}
#up-manager .upm-foot button {
  background: var(--chip); color: var(--fg); border: 0; border-radius: 8px;
  padding: 7px 14px; font-size: 12px; cursor: pointer;
}
#up-manager .upm-foot button:hover { background: #33363c; }
#up-manager .upm-foot button:disabled { opacity: .4; cursor: default; }
@media (max-width: 760px) {
  #sidebar { width: 60px; padding: 14px 6px; }
  #sidebar .logo span.txt, #sidebar nav button span.lbl, #sidebar nav .nav-count, #sidebar .foot { display: none; }
  #sidebar nav button { justify-content: center; padding: 12px 0; }
  #sidebar #search-box { margin: 2px 4px; width: calc(100% - 8px); padding: 7px 4px; font-size: 16px; }
  #lb-rail { width: 72px; padding: 10px 6px; }
  #lb-rail button { font-size: 12px; padding: 9px 2px; }
  #lb-titlebar { left: 72px; }
  #lb-content { left: 72px; }
  #lightbox img.fit, #lightbox video { max-width: calc(100vw - 72px - 8px); }
  #prev { left: 80px; }
  main { padding-left: 66px; }
  #up-bar { left: 60px; }
  #scrubber { display: none; }
}
</style>
</head>
<body>
<aside id="sidebar">
  <div class="logo"><img class="logo-icon" src="/icon/selfphotofav.png" alt=""><span class="txt">selfphoto</span><span class="ver">v.{__VERSION__}</span></div>
  <nav>
    <button id="nav-photos" class="active"><span class="ico"><svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="4" width="18" height="16" rx="2.5"/><circle cx="8.5" cy="9.5" r="1.7"/><path d="M21 16l-5-5-9 9"/></svg></span><span class="lbl">写真</span><span class="nav-count" id="nav-photos-count"></span></button>
    <button id="nav-edits"><span class="ico"><svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M4 20h4L20 8l-4-4L4 16v4z"/><path d="M13.5 6.5l4 4"/></svg></span><span class="lbl">編集写真</span><span class="nav-count" id="nav-edits-count"></span></button>
    <button id="nav-search"><span class="ico"><svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><circle cx="11" cy="11" r="6.5"/><path d="M20 20l-4.2-4.2"/></svg></span><span class="lbl">検索</span></button>
    <input id="search-box" type="search" placeholder="ファイル名・カメラで検索…" autocomplete="off">
    <button id="nav-backup"><span class="ico"><svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M12 3v12"/><path d="M6.5 10.5L12 16l5.5-5.5"/><path d="M4 16v3a2 2 0 002 2h12a2 2 0 002-2v-3"/></svg></span><span class="lbl">バックアップ</span></button>
    <button id="nav-upload"><span class="ico"><svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M12 16V4"/><path d="M6.5 9.5L12 4l5.5 5.5"/><path d="M4 20h16"/></svg></span><span class="lbl">アップロード</span></button>
    <button id="nav-select"><span class="ico"><svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><rect x="4" y="4" width="16" height="16" rx="3"/><path d="M8.5 12.5l2.5 2.5 5-5.5"/></svg></span><span class="lbl">複数選択</span></button>
    <button id="nav-download"><span class="ico"><svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M12 4v11"/><path d="M6.5 10.5L12 16l5.5-5.5"/><path d="M4 20h16"/></svg></span><span class="lbl">ダウンロード</span></button>
    <button id="nav-delete"><span class="ico"><svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M4 7h16"/><path d="M9 7V5a1 1 0 011-1h4a1 1 0 011 1v2"/><path d="M6 7l1 13a1 1 0 001 1h8a1 1 0 001-1l1-13"/></svg></span><span class="lbl">削除</span></button>
  </nav>
  <div class="foot">
    <div id="side-backup" title="バックアップの状態（クリックでバックアップ画面へ）">
      <div class="sb-row"><span class="dot" id="side-bk-dot"></span><span id="side-bk-watch">監視: -</span></div>
      <div class="sb-row"><span id="side-bk-last">前回: -</span></div>
    </div>
    <button id="restart-btn"><svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M20 12a8 8 0 1 1-2.34-5.66"/><path d="M20 3v4h-4"/></svg><span>再起動</span></button>
    <button id="update-btn"><svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M12 4v11"/><path d="M6.5 10.5L12 16l5.5-5.5"/><path d="M4 20h16"/></svg><span>アップデート</span></button>
  </div>
</aside>
<div id="restart-ov"><div class="ro-inner" id="ro-text">再起動中…</div></div>
<main>
  <div id="timeline"></div>
  <div id="loading">読み込み中…</div>
</main>
<div id="scrubber"></div>
<div id="now-viewing"></div>
<input type="file" id="file-input" multiple accept="image/*,video/*" style="display:none">
<div id="dropzone"><div class="dz-inner">ドロップでアップロード</div></div>
<div id="up-bar"><div id="up-label"></div><div id="up-track"><div id="up-fill"></div></div></div>
<div id="up-manager">
  <div class="upm-head" id="upm-head"><span id="upm-title">アップロード</span><span class="upm-count" id="upm-count"></span><span class="upm-head-btns"><button id="upm-min" title="最小化/展開">–</button><button id="upm-cancel" title="キャンセル">✕</button></span></div>
  <div class="upm-overall"><div class="upm-track" id="upm-track"><div id="upm-fill"></div></div><div id="upm-summary"></div></div>
  <div id="upm-list"></div>
  <div class="upm-foot"><button id="upm-retry" style="display:none">失敗分を再試行</button><button id="upm-close">閉じる</button></div>
</div>
<div id="ctx-menu">
  <button data-act="rot-l">左回転</button>
  <button data-act="rot-r">右回転</button>
  <button data-act="edit">編集</button>
  <button data-act="dl">ダウンロード</button>
</div>
<div id="lightbox">
  <div id="lb-rail">
    <button id="lb-zoom-in">拡大</button>
    <button id="lb-zoom-label" title="クリックで画面に合わせる">100%</button>
    <button id="lb-zoom-out">縮小</button>
    <button id="lb-copy">コピー</button>
    <button id="lb-edit">編集</button>
    <button id="lb-del">削除</button>
    <button id="lb-newtab">新タブ</button>
    <button id="lb-dl">DL</button>
    <button id="lb-prev">←</button>
    <button id="lb-next">→</button>
    <div class="rail-spacer"></div>
    <button id="lb-close">閉じる</button>
  </div>
  <div id="lb-titlebar"><span id="lb-title"></span></div>
  <button class="nav" id="prev">‹</button>
  <button class="nav" id="next">›</button>
  <div id="lb-content"></div>
</div>
<div id="editor">
  <div class="ed-head"><span id="ed-title"></span></div>
  <div class="ed-main">
    <div class="ed-canvas-wrap" id="ed-wrap"><canvas id="ed-canvas"></canvas><div id="ed-cropbox"></div></div>
    <div class="ed-side">
      <button id="ed-zoom-in">拡大</button>
      <button id="ed-zoom-label" title="クリックで画面に合わせる">100%</button>
      <button id="ed-zoom-out">縮小</button>
      <button id="ed-rot-l">左回転</button>
      <button id="ed-rot-r">右回転</button>
      <button data-tool="rect">赤枠挿入</button>
      <button data-tool="arrow">矢印挿入</button>
      <button data-tool="pen">自由線</button>
      <button data-tool="crop">トリミング</button>
      <button data-tool="mosaic">モザイク</button>
      <button data-tool="blur">ぼかし</button>
      <button data-tool="resize">リサイズ</button>
      <button id="ed-rename">リネーム</button>
      <div class="ed-panel" id="ed-panel-rect">
        <div class="ed-hint">ドラッグした範囲に角丸の赤枠</div>
        <label>太さ <input type="range" id="ed-rect-width" min="1" max="5" step="1" value="3"><span id="ed-rect-width-v">3</span></label>
      </div>
      <div class="ed-panel" id="ed-panel-arrow">
        <div class="ed-hint">ドラッグした方向・長さで赤矢印</div>
        <label>太さ <input type="range" id="ed-arrow-width" min="1" max="5" step="1" value="3"><span id="ed-arrow-width-v">3</span></label>
      </div>
      <div class="ed-panel" id="ed-panel-pen">
        <div class="ed-hint">ドラッグした軌跡に赤色の自由線</div>
        <label>太さ <input type="range" id="ed-pen-width" min="1" max="5" step="1" value="3"><span id="ed-pen-width-v">3</span></label>
      </div>
      <div class="ed-panel" id="ed-panel-crop">
        <label><input type="radio" name="ed-ratio" value="keep" checked> 比率維持</label>
        <label><input type="radio" name="ed-ratio" value="free"> 自由選択</label>
        <div class="ed-row"><button id="ed-crop-apply">適用</button><button id="ed-crop-clear">クリア</button></div>
        <div class="ed-hint">画像上でドラッグして範囲選択</div>
      </div>
      <div class="ed-panel" id="ed-panel-mosaic">
        <div class="ed-hint">塗った場所にモザイク</div>
        <label>強度 <input type="range" id="ed-mosaic-strength" min="1" max="5" step="1" value="3"><span id="ed-mosaic-strength-v">3</span></label>
        <label>太さ <input type="range" id="ed-mosaic-size" min="1" max="5" step="1" value="3"><span id="ed-mosaic-size-v">3</span></label>
      </div>
      <div class="ed-panel" id="ed-panel-blur">
        <div class="ed-hint">塗った場所をぼかし</div>
        <label>強度 <input type="range" id="ed-blur-strength" min="1" max="5" step="1" value="3"><span id="ed-blur-strength-v">3</span></label>
        <label>太さ <input type="range" id="ed-blur-size" min="1" max="5" step="1" value="3"><span id="ed-blur-size-v">3</span></label>
      </div>
      <div class="ed-panel" id="ed-panel-resize">
        <div class="ed-hint">いずれか1つを入力（縦横比は維持）</div>
        <label>長辺 <input type="number" id="ed-rs-long" min="1" max="8192" placeholder="px"></label>
        <label>横幅 <input type="number" id="ed-rs-w" min="1" max="8192" placeholder="px"></label>
        <div class="ed-row" style="justify-content:flex-end"><button id="ed-rs-swap" title="横幅と縦幅を入れ替え">⇄</button></div>
        <label>縦幅 <input type="number" id="ed-rs-h" min="1" max="8192" placeholder="px"></label>
        <div class="ed-hint">横幅プリセット</div>
        <div class="ed-row">
          <button data-w="1980">1980</button><button data-w="1280">1280</button><button data-w="1024">1024</button><button data-w="320">320</button>
        </div>
        <div class="ed-row"><button id="ed-rs-apply">適用</button></div>
        <div class="ed-hint" id="ed-rs-cur"></div>
      </div>
      <button id="ed-undo">1つ戻す</button>
    </div>
  </div>
  <div class="ed-savebar">
    <button id="ed-overwrite">上書き保存</button>
    <button id="ed-saveas">別名保存</button>
    <button id="ed-tolibrary">編集フォルダに保存</button>
    <button id="ed-close">閉じる</button>
  </div>
</div>
<script>
const state = { view: 'photos', month: null, term: '', offset: 0, limit: 500, done: false, photos: [], selecting: false, selected: new Set(),
  // loadPhotos の多重起動防止フラグと、view 切替時の旧 fetch 破棄用世代番号
  loading: false, gen: 0,
  // 明示的にチェックされた見出し（フォルダ・月）。ファイル個別チェックでは付けない。
  // フォルダ見出しキー = 日付フォルダ（年/年月/年月日_）、月見出しキー = prefix（年/年月/）。
  folderSel: new Set(), monthSel: new Set() };
const fmt = d => {
  const t = new Date(d);
  return `${t.getFullYear()}年${t.getMonth()+1}月`;
};
const fday = d => {
  const t = new Date(d);
  return `${t.getFullYear()}年${t.getMonth()+1}月${t.getDate()}日`;
};

// ---------------- sidebar navigation ----------------
const searchBox = document.getElementById('search-box');
const VIEW_TITLES = { photos: '写真', edits: '編集写真', search: '検索', backup: 'バックアップ' };
let searchTimer = null;
let backupTimer = null;

document.getElementById('nav-photos').onclick = () => setView('photos');
document.getElementById('nav-edits').onclick = () => setView('edits');
document.getElementById('nav-search').onclick = () => { setView('search'); searchBox.focus(); };
document.getElementById('nav-backup').onclick = () => setView('backup');
document.getElementById('nav-upload').onclick = () => fileInput.click();
document.getElementById('side-backup').onclick = () => setView('backup');

function setView(v) {
  state.view = v;
  window.scrollTo(0, 0); // 画面切替は先頭から表示する
  if (backupTimer) { clearInterval(backupTimer); backupTimer = null; }
  document.querySelectorAll('#sidebar nav button').forEach(b => b.classList.remove('active'));
  document.getElementById('nav-' + v).classList.add('active');
  document.title = 'selfphoto - ' + (VIEW_TITLES[v] || v);
  searchBox.style.display = v === 'search' ? 'block' : 'none';
  if (v === 'search') {
    if (!state.term) state.term = '';
    reload();
  } else if (v === 'backup') {
    reloadBackup();
  } else {
    state.term = '';
    searchBox.value = '';
    reload();
  }
  if (v !== 'backup') loadMonths();
}

searchBox.addEventListener('input', () => {
  clearTimeout(searchTimer);
  searchTimer = setTimeout(() => {
    state.term = searchBox.value.trim();
    if (state.view !== 'search') setView('search');
    else reload();
  }, 250);
});

async function loadMonths() {
  const r = await fetch('/api/months');
  const j = await r.json();
  const pc = document.getElementById('nav-photos-count');
  if (pc) pc.textContent = (j.total ?? '') === '' ? '' : `${j.total}`;
  const ec = document.getElementById('nav-edits-count');
  if (ec) ec.textContent = (j.edits ?? '') === '' ? '' : `${j.edits}`;
}

// ---------------- backup settings ----------------
const WATCH_LABELS = { 60: '1分ごと', 300: '5分ごと', 900: '15分ごと', 1800: '30分ごと', 3600: '1時間ごと' };
function reloadBackup() {
  state.gen++; // 旧 fetch の結果は破棄される
  state.loading = false;
  state.photos = []; state.offset = 0; state.done = true;
  state.selected.clear(); state.folderSel.clear(); state.monthSel.clear();
  // 写真一覧用の年月スクラバーはバックアップ画面では不要なので隠す
  // （render→buildScrubber で他ビュー表示時に復帰する）。
  // 参照リストも空にしないと、切り離し済み見出しの座標が 0 扱いになり
  // スクロール時に古い年月表示（#now-viewing）が出てしまう。
  scrubberItems = [];
  document.getElementById('scrubber').style.display = 'none';
  renderBackup();
  refreshBackupStatus();
  if (backupTimer) clearInterval(backupTimer);
  backupTimer = setInterval(() => { if (state.view === 'backup') refreshBackupStatus(); }, 3000);
}
function renderBackup() {
  const tl = document.getElementById('timeline');
  tl.innerHTML = '';
  document.getElementById('loading').textContent = '';
  const wrap = document.createElement('div');
  wrap.id = 'backup-form';
  wrap.innerHTML = `
    <h2>バックアップ設定</h2>
    <p class="bk-desc">rsync で写真フォルダをコピーします（オプション固定: <code>-r -t -u -v --progress</code>）。</p>
    <label>ソースフォルダ<input id="bk-source" type="text"></label>
    <label>ターゲットフォルダ<input id="bk-target" type="text" placeholder="/mnt/backup/selfphoto または SSH時はリモートパス"></label>
    <div class="bk-row"><button id="bk-target-check" type="button">フォルダ確認</button><span id="bk-target-msg" class="bk-msg"></span></div>
    <div class="bk-desc">無い場合は自動で作成します（ローカルは <code>mkdir -p</code>、SSH先はリモートで <code>mkdir -p</code>）。</div>
    <div class="bk-row">除外: <code>.upload-tmp/</code>（固定）</div>
    <fieldset><legend>SSHリモート接続</legend>
      <label class="bk-check"><input id="bk-ssh-on" type="checkbox"> SSH経由で転送する</label>
      <div id="bk-ssh-fields">
        <label>ホスト<input id="bk-ssh-host" type="text" placeholder="例: 192.0.2.10"></label>
        <label>ユーザー<input id="bk-ssh-user" type="text"></label>
        <label>ポート<input id="bk-ssh-port" type="text" placeholder="22"></label>
        <label>鍵ファイル<input id="bk-ssh-key" type="text" placeholder="例: /root/.ssh/id_rsa（任意）"></label>
        <label>パスワード<input id="bk-ssh-pw" type="password" placeholder="変更しない場合は空欄"></label>
      </div>
      <div class="bk-row"><button id="bk-ssh-test" type="button">接続確認</button><button id="bk-ssh-save" type="button">設定保存</button><span id="bk-ssh-msg" class="bk-msg"></span></div>
      <div class="bk-desc" id="bk-ssh-diag"></div>
      <div class="bk-desc">接続エラーとコピーエラーの切り分け用。先に「接続確認」でSSH疎通を確かめられます。</div>
    </fieldset>
    <fieldset><legend>監視（自動実行）</legend>
      <div class="bk-desc">保存・取込で写真が増えたら自動コピーします。間隔実行か、夜間などの指定時刻実行かを選べます（rsyncgui と同じ形）。</div>
      <label>方式<select id="bk-mode"><option value="interval">間隔で実行</option><option value="time">指定時刻に実行</option></select></label>
      <div id="bk-interval-row"><label>間隔<select id="bk-interval"></select></label></div>
      <div id="bk-time-row" style="display:flex;flex-direction:column;gap:8px">
        <div class="bk-row">時刻 <span id="bk-times"></span><button id="bk-time-add" type="button">+ 時刻追加</button></div>
        <div class="bk-row">曜日 <span id="bk-days"></span></div>
        <div class="bk-desc">指定した曜日・時刻になったときに未コピー分があれば実行します（無ければ何もしません）。</div>
      </div>
    </fieldset>
    <div class="bk-row">
      <button id="bk-save">設定を保存</button>
      <button id="bk-run">コピー実行</button>
      <button id="bk-watch">監視開始</button>
    </div>
    <div id="bk-status"></div>
    <h3>実行ログ（最新）</h3>
    <pre id="bk-log"></pre>`;
  tl.appendChild(wrap);
  document.getElementById('bk-save').onclick = saveBackupConfig;
  document.getElementById('bk-ssh-save').onclick = saveBackupConfig;
  document.getElementById('bk-ssh-test').onclick = testSshConnection;
  document.getElementById('bk-target-check').onclick = checkTargetFolder;
  document.getElementById('bk-run').onclick = runBackupNow;
  document.getElementById('bk-watch').onclick = toggleBackupWatch;
  document.getElementById('bk-time-add').onclick = () => { bkAddTime('12:00'); };
  document.getElementById('bk-mode').onchange = bkUpdateModeVisibility;
  fetch('/api/backup-config').then(r => r.json()).then(j => {
    if (state.view !== 'backup') return;
    document.getElementById('bk-source').value = j.source || '';
    document.getElementById('bk-target').value = j.target || '';
    const ssh = j.ssh || {};
    document.getElementById('bk-ssh-on').checked = !!ssh.enabled;
    document.getElementById('bk-ssh-host').value = ssh.host || '';
    document.getElementById('bk-ssh-user').value = ssh.user || '';
    document.getElementById('bk-ssh-port').value = ssh.port || '22';
    document.getElementById('bk-ssh-key').value = ssh.key || '';
    const sel = document.getElementById('bk-interval');
    sel.innerHTML = '';
    (j.intervals || [300]).forEach(iv => {
      const o = document.createElement('option');
      o.value = iv; o.textContent = WATCH_LABELS[iv] || `${iv}秒ごと`;
      sel.appendChild(o);
    });
    const w = j.watch || {};
    sel.value = String(w.intervalSec || 300);
    document.getElementById('bk-mode').value = (w.mode === 'time') ? 'time' : 'interval';
    bkRenderTimes(Array.isArray(w.times) && w.times.length ? w.times : ['02:00']);
    bkRenderDays(Array.isArray(w.days) ? w.days : [0,1,2,3,4,5,6]);
    bkUpdateModeVisibility();
    refreshBackupStatus();
  }).catch(() => {
    document.getElementById('bk-status').textContent = '設定を取得できませんでした';
  });
}
const BK_DAY_NAMES = ['日','月','火','水','木','金','土'];
function bkUpdateModeVisibility() {
  const modeEl = document.getElementById('bk-mode');
  const mode = modeEl ? modeEl.value : 'interval';
  const ivRow = document.getElementById('bk-interval-row');
  const tmRow = document.getElementById('bk-time-row');
  if (ivRow) ivRow.style.display = (mode === 'time') ? 'none' : '';
  if (tmRow) tmRow.style.display = (mode === 'time') ? '' : 'none';
}
function bkRenderTimes(times) {
  const box = document.getElementById('bk-times');
  if (!box) return;
  box.innerHTML = '';
  const list = (Array.isArray(times) && times.length ? times : ['02:00']).slice(0, 10);
  list.forEach((t, i) => {
    const row = document.createElement('span');
    row.className = 'bk-time-row';
    row.style.cssText = 'display:inline-flex;gap:4px;align-items:center;margin-right:6px;margin-bottom:4px';
    const inp = document.createElement('input');
    inp.type = 'time'; inp.value = t; inp.dataset.idx = String(i);
    inp.onchange = () => {};
    row.appendChild(inp);
    if (list.length > 1) {
      const del = document.createElement('button');
      del.type = 'button'; del.textContent = '✕';
      del.onclick = () => { row.remove(); };
      row.appendChild(del);
    }
    box.appendChild(row);
  });
}
function bkAddTime(v) {
  const box = document.getElementById('bk-times');
  if (!box) return;
  const cur = bkCollectTimes();
  if (cur.length >= 10) return;
  cur.push(v || '12:00');
  bkRenderTimes(cur);
}
function bkCollectTimes() {
  const box = document.getElementById('bk-times');
  if (!box) return ['02:00'];
  const vals = [...box.querySelectorAll('input[type="time"]')].map(el => el.value).filter(Boolean);
  return vals.length ? vals : ['02:00'];
}
function bkRenderDays(days) {
  const box = document.getElementById('bk-days');
  if (!box) return;
  const set = new Set((Array.isArray(days) ? days : []).map(Number));
  box.innerHTML = '';
  BK_DAY_NAMES.forEach((name, i) => {
    const b = document.createElement('span');
    b.className = 'day-btn' + (set.has(i) ? ' on' : '');
    b.textContent = name;
    b.dataset.day = String(i);
    b.onclick = () => { b.classList.toggle('on'); };
    box.appendChild(b);
  });
}
function bkCollectDays() {
  const box = document.getElementById('bk-days');
  if (!box) return [0,1,2,3,4,5,6];
  const vals = [...box.querySelectorAll('.day-btn.on')].map(el => parseInt(el.dataset.day, 10));
  return vals.length ? vals.sort() : [0,1,2,3,4,5,6];
}
function bkWatchSummary(w) {
  if (!w) return '';
  if (w.mode === 'time') {
    const times = (w.times || ['02:00']).join(' / ');
    const days = (w.days || []).map(d => BK_DAY_NAMES[d] || '').join('・');
    return `指定時刻 ${days} ${times}`;
  }
  const iv = w.intervalSec || 300;
  return WATCH_LABELS[iv] || `${iv}秒ごと`;
}
function backupFormValues() {
  return {
    source: document.getElementById('bk-source').value,
    target: document.getElementById('bk-target').value,
    ssh: {
      enabled: document.getElementById('bk-ssh-on').checked,
      host: document.getElementById('bk-ssh-host').value,
      user: document.getElementById('bk-ssh-user').value,
      port: document.getElementById('bk-ssh-port').value || '22',
      key: document.getElementById('bk-ssh-key').value,
      password: document.getElementById('bk-ssh-pw').value,
    },
    watch: {
      mode: document.getElementById('bk-mode').value === 'time' ? 'time' : 'interval',
      intervalSec: parseInt(document.getElementById('bk-interval').value, 10) || 300,
      times: bkCollectTimes(),
      days: bkCollectDays(),
    },
  };
}
async function saveBackupConfig() {
  const body = backupFormValues();
  // パスワード空欄は「変更なし」の意味。**** は送らない。
  const r = await fetch('/api/backup-config', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });
  const j = await r.json();
  if (!j.ok) { alert('保存に失敗しました: ' + (j.error || 'unknown')); return; }
  document.getElementById('bk-ssh-pw').value = '';
  const sm = document.getElementById('bk-ssh-msg');
  if (sm) { sm.textContent = '保存しました'; sm.className = 'bk-msg ok'; }
  alert('保存しました');
  refreshBackupStatus();
}
function setBkMsg(id, ok, text) {
  const el = document.getElementById(id);
  if (!el) return;
  el.textContent = text;
  el.className = 'bk-msg ' + (ok ? 'ok' : 'ng');
}
async function testSshConnection() {
  const body = backupFormValues();
  setBkMsg('bk-ssh-msg', true, '確認中…');
  const diagEl = document.getElementById('bk-ssh-diag');
  if (diagEl) diagEl.textContent = '';
  try {
    const r = await fetch('/api/backup-ssh-test', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ ssh: body.ssh }),
    });
    const j = await r.json();
    if (j.ok) setBkMsg('bk-ssh-msg', true, 'OK: ' + (j.message || 'SSH接続OK'));
    else setBkMsg('bk-ssh-msg', false, 'NG(接続エラー): ' + (j.error || 'unknown'));
    const d = j.diagnostics;
    if (d && diagEl) {
      const keyTxt = (d.key && d.key.specified)
        ? `鍵ファイル: ${d.key.path} (${d.key.exists ? (d.key.readable ? 'あり・読める' : 'あり・読めない') : 'なし'})`
        : `鍵ファイル: 未指定 (サーバー既定の鍵: ${(d.defaultKeys || []).join(', ') || 'なし'})`;
      diagEl.textContent =
        `診断: 実行ユーザー=${d.runUser} / sshpass=${d.sshpassAvailable ? 'あり' : 'なし'} / ${keyTxt}`;
    }
  } catch (err) {
    setBkMsg('bk-ssh-msg', false, 'NG(接続エラー): ' + err);
  }
}
async function checkTargetFolder() {
  const body = backupFormValues();
  setBkMsg('bk-target-msg', true, '確認中…');
  try {
    const r = await fetch('/api/backup-target-check', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ target: body.target, ssh: body.ssh }),
    });
    const j = await r.json();
    if (j.ok) setBkMsg('bk-target-msg', true, 'OK: ' + (j.message || 'フォルダOK'));
    else setBkMsg('bk-target-msg', false, 'NG: ' + (j.error || 'unknown'));
  } catch (err) {
    setBkMsg('bk-target-msg', false, 'NG: ' + err);
  }
}
async function runBackupNow() {
  const r = await fetch('/api/backup-run', { method: 'POST' });
  const j = await r.json();
  if (!j.ok && !j.started) { alert('実行できませんでした: ' + (j.error || 'unknown')); return; }
  refreshBackupStatus();
}
async function toggleBackupWatch() {
  const st = await (await fetch('/api/backup-status')).json();
  const r = await fetch('/api/backup-watch', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ enabled: !st.watching }),
  });
  const j = await r.json();
  if (!j.ok) { alert('切替に失敗しました'); return; }
  refreshBackupStatus();
}
async function refreshBackupStatus() {
  if (state.view !== 'backup') return;
  const stEl = document.getElementById('bk-status');
  const logEl = document.getElementById('bk-log');
  const watchBtn = document.getElementById('bk-watch');
  if (!stEl) return;
  let st;
  try {
    st = await (await fetch('/api/backup-status')).json();
  } catch (err) {
    stEl.textContent = '状態を取得できませんでした';
    return;
  }
  if (!st.rsyncAvailable) {
    stEl.innerHTML = '警告: rsync が見つかりません（<code>apt install rsync</code> 等で導入してください）';
  } else {
    const last = st.lastRun;
    const lastTxt = last
      ? `前回: ${last.ok ? '成功' : '失敗'}（${new Date(last.finishedAt * 1000).toLocaleString()}）`
      : '前回: まだ実行していません';
    const wsum = bkWatchSummary(st.watch);
    stEl.textContent = `監視: ${st.watching ? 'ON(' + wsum + ')' : 'OFF(' + wsum + ')'} / 状態: ${st.running ? '実行中…' : (st.dirty ? '未コピーあり' : '待機中')} / ${lastTxt}`;
  }
  if (watchBtn) watchBtn.textContent = st.watching ? '監視停止' : '監視開始';
  if (logEl) logEl.textContent = (st.lastRun && st.lastRun.logTail) || '(ログなし)';
  updateSidebarBackup(st);
}

// ---------------- sidebar backup status ----------------
function updateSidebarBackup(st) {
  const wEl = document.getElementById('side-bk-watch');
  const lEl = document.getElementById('side-bk-last');
  const dot = document.getElementById('side-bk-dot');
  if (!wEl || !lEl || !st) return;
  wEl.textContent = `監視: ${st.watching ? 'ON(' + bkWatchSummary(st.watch) + ')' : 'OFF'}`;
  const last = st.lastRun;
  lEl.textContent = last
    ? `前回: ${last.ok ? '成功' : '失敗'} ${new Date(last.finishedAt * 1000).toLocaleString('ja-JP', { month: 'numeric', day: 'numeric', hour: '2-digit', minute: '2-digit' })}`
    : '前回: 未実行';
  if (dot) dot.className = 'dot ' + (st.running ? 'run' : (last ? (last.ok ? 'ok' : 'ng') : (st.watching ? 'on' : '')));
}
async function refreshSidebarBackup() {
  let st;
  try {
    st = await (await fetch('/api/backup-status')).json();
  } catch (err) {
    return;
  }
  updateSidebarBackup(st);
}

async function loadPhotos() {
  if (state.view === 'backup' || state.done || state.loading) return;
  const gen = state.gen;
  state.loading = true;
  document.getElementById('loading').textContent = '読み込み中…';
  try {
    if (state.view === 'edits') {
      // 編集画像フォルダは全件一括（件数は少ない想定）
      const r = await fetch('/api/edits');
      if (gen !== state.gen) return; // view 切替後の旧 fetch は破棄
      const j = await r.json();
      if (gen !== state.gen) return;
      state.photos.push(...(j.photos || []));
      state.done = true;
      render();
      document.getElementById('loading').textContent = '';
      return;
    }
    const q = new URLSearchParams({ limit: state.limit, offset: state.offset });
    if (state.view === 'search') {
      q.set('q', state.term);
    }
    const url = state.view === 'search' ? '/api/search' : '/api/photos';
    const r = await fetch(url + '?' + q);
    if (gen !== state.gen) return; // view 切替後の旧 fetch は破棄
    const j = await r.json();
    if (gen !== state.gen) return;
    if (j.photos.length < state.limit) state.done = true;
    state.offset += j.photos.length;
    state.photos.push(...j.photos);
    render();
    document.getElementById('loading').textContent = state.done ? '' : 'もっと読み込む…';
  } finally {
    // 新しい世代の読み込み中はフラグを落とさない
    if (gen === state.gen) state.loading = false;
  }
}

function render() {
  const tl = document.getElementById('timeline');
  tl.innerHTML = '';
  let curMonth = '', curDay = '', grid = null;
  for (const p of state.photos) {
    const d = new Date(p.capturedLocal);
    const ym = `${d.getFullYear()}年${String(d.getMonth()+1).padStart(2,'0')}月`;
    if (ym !== curMonth) {
      curMonth = ym; curDay = '';
      const h = document.createElement('div');
      h.className = 'month-head'; h.dataset.ym = `${d.getFullYear()}${String(d.getMonth()+1).padStart(2,'0')}`;
      const mhBox = makeHeadBox('month', `${d.getFullYear()}/${String(d.getMonth()+1).padStart(2,'0')}`);
      if (mhBox) h.appendChild(mhBox);
      h.appendChild(Object.assign(document.createElement('span'), { textContent: ym }));
      tl.appendChild(h);
      grid = null;
    }
    const day = fday(p.capturedLocal);
    if (day !== curDay) {
      curDay = day;
      const dh = document.createElement('div');
      dh.className = 'day-head';
      dh.style.cssText = 'padding:6px 10px 4px;color:var(--muted);font-size:12px';
      // 日付見出しに対応するフォルダ（年/年月/年月日_）を求める
      const fd = folderOfDay(p);
      if (fd) {
        dh.dataset.folder = fd.folder;
        const dhBox = makeHeadBox('day', fd.folder, fd.paths);
        if (dhBox) dh.appendChild(dhBox);
      }
      dh.appendChild(Object.assign(document.createElement('span'), { textContent: day }));
      tl.appendChild(dh);
      grid = document.createElement('div');
      grid.className = 'grid';
      tl.appendChild(grid);
    }
    grid.appendChild(makeCell(p));
  }
  buildScrubber();
  refreshSelectionUi();
}

// ---------------- selection mode ----------------
function folderOfDay(p) {
  // path の 先頭3セグメント（年/年月/年月日_）を日付フォルダとみなす
  const seg = p.path.split('/');
  if (seg.length < 3) return null;
  const folder = seg.slice(0, 3).join('/');
  const paths = state.photos.filter(x => x.path.startsWith(folder + '/')).map(x => x.path);
  return { folder, paths };
}

function makeHeadBox(kind, key, paths) {
  // 常に生成する（表示は body.selecting の CSS で切り替える）。
  // 選択モード突入時に DOM を作り直さなくても済むようにするため。
  const box = document.createElement('div');
  box.className = 'sel-box';
  box.textContent = '';
  if (kind === 'month') {
    // key = "YYYY/MM" → フォルダ prefix "YYYY/YYYYMM/"
    const y = key.slice(0, 4);
    const m = key.slice(5);
    const prefix = `${y}/${y}${m}/`;
    box.addEventListener('click', e => {
      e.stopPropagation();
      const inMonth = state.photos.filter(p => p.path.startsWith(prefix));
      if (state.monthSel.has(prefix)) {
        // 明示チェック解除：月・配下フォルダの明示を外し、ファイル選択も外す
        state.monthSel.delete(prefix);
        inMonth.forEach(p => {
          state.selected.delete(p.path);
          state.folderSel.delete(p.path.split('/').slice(0, 3).join('/'));
        });
      } else {
        // 明示チェック：月・配下フォルダを明示扱いにし、ファイルを全選択
        state.monthSel.add(prefix);
        inMonth.forEach(p => {
          state.selected.add(p.path);
          state.folderSel.add(p.path.split('/').slice(0, 3).join('/'));
        });
      }
      refreshSelectionUi();
    });
  } else {
    box.addEventListener('click', e => {
      e.stopPropagation();
      if (state.folderSel.has(key)) {
        // 明示チェック解除：フォルダの明示を外し、ファイル選択も外す
        state.folderSel.delete(key);
        paths.forEach(p => state.selected.delete(p));
      } else {
        // 明示チェック：フォルダを明示扱いにし、ファイルを全選択
        state.folderSel.add(key);
        paths.forEach(p => state.selected.add(p));
      }
      refreshSelectionUi();
    });
  }
  return box;
}

function refreshSelectionUi() {
  document.body.classList.toggle('selecting', state.selecting);
  const selLbl = document.querySelector('#nav-select .lbl');
  if (selLbl) selLbl.textContent = state.selecting ? '解除' : '複数選択';
  document.getElementById('nav-select').classList.toggle('on', state.selecting);
  document.getElementById('nav-download').style.display = state.selecting ? 'flex' : 'none';
  document.getElementById('nav-delete').style.display = state.selecting ? 'flex' : 'none';
  // セルの表示更新
  document.querySelectorAll('.cell').forEach(c => {
    c.classList.toggle('selected', state.selected.has(c.dataset.path));
  });
  // 見出しのチェック表示更新
  // ✓ は見出しを明示チェックした場合のみ。ファイル個別チェックだけでは付けない
  // （フォルダ丸ごとの zip ダウンロードと区別するため）。件数表示は従来通り。
  document.querySelectorAll('.day-head').forEach(dh => {
    const folder = dh.dataset.folder;
    if (!folder) return;
    const inFolder = state.photos.filter(p => p.path.startsWith(folder + '/')).map(p => p.path);
    const n = inFolder.filter(p => state.selected.has(p)).length;
    const box = dh.querySelector('.sel-box');
    if (box) {
      box.textContent = state.folderSel.has(folder) ? '✓' : (n === 0 ? '' : String(n));
      dh.classList.toggle('on', n > 0);
    }
  });
  document.querySelectorAll('.month-head').forEach(mh => {
    const ym = mh.dataset.ym;
    const folderPrefix = `${ym.slice(0,4)}/${ym}/`;
    const inMonth = state.photos.filter(p => p.path.startsWith(folderPrefix)).map(p => p.path);
    const n = inMonth.filter(p => state.selected.has(p)).length;
    const box = mh.querySelector('.sel-box');
    if (box) {
      box.textContent = state.monthSel.has(folderPrefix) ? '✓' : (n === 0 ? '' : String(n));
      mh.classList.toggle('on', n > 0);
    }
  });
}

function makeCell(p) {
  const c = document.createElement('div');
  c.className = 'cell';
  c.dataset.path = p.path;
  // DBの縦横比でセルの形を決める（横長は横長・縦長は縦長）。高さはCSSで統一。
  // 極端なパノラマ・細長画像は崩れ防止に 0.5〜3.0 に丸める。
  let ratio = 1;
  if (p.width && p.height) ratio = p.width / p.height;
  ratio = Math.min(3, Math.max(0.5, ratio));
  c.style.aspectRatio = ratio;
  const img = document.createElement('img');
  img.loading = 'lazy';
  img.dataset.src = p.thumb || p.original;
  img.alt = p.filename;
  img.addEventListener('click', () => {
    if (state.selecting) { toggleSel(p); return; }
    openLb(p);
  });
  c.addEventListener('contextmenu', e => {
    e.preventDefault();
    e.stopPropagation();
    showCtxMenu(e.clientX, e.clientY, p);
  });
  const box = document.createElement('div');
  box.className = 'sel-box';
  box.textContent = '';
  box.addEventListener('click', e => { e.stopPropagation(); toggleSel(p); });
  c.appendChild(img);
  c.appendChild(box);
  if (p.isVideo) {
    const b = document.createElement('div');
    b.className = 'badge'; b.textContent = '▶';
    c.appendChild(b);
  }
  lazyObserver.observe(img);
  return c;
}

function toggleSel(p) {
  if (state.selected.has(p.path)) state.selected.delete(p.path);
  else state.selected.add(p.path);
  // ファイル個別操作では見出しの明示チェックを付けない。逆に外れた場合は
  // 所属フォルダ・月の明示を解除する（全選択状態が崩れるため）。
  const seg = p.path.split('/');
  if (seg.length >= 3) state.folderSel.delete(seg.slice(0, 3).join('/'));
  if (seg.length >= 2) state.monthSel.delete(seg.slice(0, 2).join('/') + '/');
  refreshSelectionUi();
}

// ---------------- sidebar selection ----------------
document.getElementById('nav-select').addEventListener('click', () => {
  state.selecting = !state.selecting;
  if (!state.selecting) { state.selected.clear(); state.folderSel.clear(); state.monthSel.clear(); }
  refreshSelectionUi();
});

// ---------------- restart (sidebar) ----------------
document.getElementById('restart-btn').addEventListener('click', async () => {
  if (!confirm('selfphoto サービスを再起動しますか？')) return;
  const ov = document.getElementById('restart-ov');
  document.getElementById('ro-text').textContent = '再起動中…';
  ov.classList.add('on');
  try {
    const r = await fetch('/api/restart', { method: 'POST' });
    if (!r.ok) {
      const j = await r.json().catch(() => ({}));
      alert('再起動に失敗しました: ' + (j.error || r.status));
      ov.classList.remove('on');
      return;
    }
  } catch (err) {
    // 再起動が先に走って接続が切れることはある。その場合は続行。
  }
  // サーバが上がってくるまでポーリング（最大 30 秒）
  const deadline = Date.now() + 30000;
  while (Date.now() < deadline) {
    await new Promise(res => setTimeout(res, 1000));
    try {
      const r = await fetch('/healthz', { cache: 'no-store' });
      if (r.ok) { location.reload(); return; }
    } catch (err) { /* まだ上がっていない */ }
  }
  ov.classList.remove('on');
  alert('サーバの復帰を確認できませんでした。時間をおいて再読み込みしてください。');
});

// ---------------- update (sidebar) ----------------
document.getElementById('update-btn').addEventListener('click', async () => {
  if (!confirm('GitHub から最新版を取得してアップデートしますか？')) return;
  const ov = document.getElementById('restart-ov');
  const roText = document.getElementById('ro-text');
  roText.textContent = '更新中…';
  ov.classList.add('on');
  try {
    const r = await fetch('/api/update', { method: 'POST' });
    const j = await r.json().catch(() => ({}));
    if (!r.ok || !j.ok) {
      alert('アップデートに失敗しました: ' + (j.error || r.status));
      ov.classList.remove('on');
      return;
    }
    if (!j.restarting) {
      alert('更新しました。サーバを手動で再起動してください。');
      ov.classList.remove('on');
      return;
    }
  } catch (err) {
    // 再起動が先に走って接続が切れることはある。その場合は続行。
  }
  // サーバが上がってくるまでポーリング（更新は時間がかかるため最大 5 分）
  roText.textContent = '再起動中…';
  const deadline = Date.now() + 300000;
  while (Date.now() < deadline) {
    await new Promise(res => setTimeout(res, 2000));
    try {
      const r = await fetch('/healthz', { cache: 'no-store' });
      if (r.ok) { location.reload(); return; }
    } catch (err) { /* まだ上がっていない */ }
  }
  ov.classList.remove('on');
  alert('サーバの復帰を確認できませんでした。時間をおいて再読み込みしてください。');
});

document.getElementById('nav-delete').addEventListener('click', async () => {
  const sel = state.photos.filter(p => state.selected.has(p.path));
  if (!sel.length) { alert('削除する写真を選択してください'); return; }
  if (!confirm(`${sel.length}件を削除しますか？\n（一覧・ファイル実体・サムネイルから削除されます。元に戻せません）`)) return;
  const paths = sel.map(p => p.path);
  let j = null;
  try {
    const r = await fetch('/api/delete', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ paths }),
    });
    j = await r.json();
  } catch (err) {
    alert('削除に失敗しました（通信エラー）');
    return;
  }
  if (!j || !j.ok) {
    alert('削除に失敗しました: ' + ((j && j.error) || 'unknown error'));
    return;
  }
  const gone = new Set(j.deleted || []);
  if (gone.size) {
    state.photos = state.photos.filter(p => !gone.has(p.path));
    state.selected = new Set([...state.selected].filter(p => !gone.has(p)));
    // 写真が残っていないフォルダ・月の明示チェックは外す
    state.folderSel = new Set([...state.folderSel].filter(f =>
      state.photos.some(p => p.path.startsWith(f + '/'))));
    state.monthSel = new Set([...state.monthSel].filter(m =>
      state.photos.some(p => p.path.startsWith(m))));
    render();
    loadMonths();
  }
  if (j.errors && j.errors.length) {
    alert(`${gone.size}件を削除しました。${j.errors.length}件は失敗しました`);
  }
});

document.getElementById('nav-download').addEventListener('click', async () => {
  const sel = state.photos.filter(p => state.selected.has(p.path));
  if (!sel.length) { alert('ダウンロードする写真を選択してください'); return; }
  // 日付フォルダごとにグループ化（フォルダ見出しを明示チェックした場合のみ zip、
  // ファイル個別チェックだけの場合は全ファイルでも 1 枚ずつ）
  const byFolder = new Map();
  for (const p of sel) {
    const seg = p.path.split('/');
    const folder = seg.length >= 3 ? seg.slice(0, 3).join('/') : '';
    if (!byFolder.has(folder)) byFolder.set(folder, []);
    byFolder.get(folder).push(p);
  }
  for (const [folder, items] of byFolder) {
    // フォルダ見出し・月見出しの明示チェックがある場合のみ zip 1 つで
    const fseg = folder ? folder.split('/') : [];
    const monthPrefix = fseg.length >= 2 ? fseg.slice(0, 2).join('/') + '/' : '';
    const folderChecked = folder && (state.folderSel.has(folder) || (monthPrefix && state.monthSel.has(monthPrefix)));
    if (folderChecked) {
      const fname = folder.split('/').pop() || 'photos';
      await downloadUrl(`/api/zip?prefix=${encodeURIComponent(folder)}&name=${encodeURIComponent(fname)}`);
    } else {
      for (const p of items) {
        await downloadUrl(p.original, p.filename);
      }
    }
  }
});

async function downloadUrl(url, filename) {
  const r = await fetch(url);
  if (!r.ok) { console.error('download failed', url, r.status); return; }
  const blob = await r.blob();
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  if (filename) a.download = filename;
  else {
    const cd = r.headers.get('Content-Disposition') || '';
    const m = cd.match(/filename\*=UTF-8''([^;]+)/);
    if (m) a.download = decodeURIComponent(m[1]);
  }
  document.body.appendChild(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(a.href), 30000);
}

// ---------------- thumbnail context menu ----------------
// サムネイル一覧の右クリックメニュー（左回転・右回転・編集・ダウンロード）。
// 回転はサーバ側で元画像を90度回転し、EXIF維持で上書き保存する。
let ctxPhoto = null;
const ctxMenu = document.getElementById('ctx-menu');
function hideCtxMenu() {
  if (ctxMenu) ctxMenu.classList.remove('on');
  ctxPhoto = null;
}
function showCtxMenu(x, y, p) {
  if (!ctxMenu) return;
  ctxPhoto = p;
  const isVideo = !!p.isVideo;
  ctxMenu.querySelectorAll('button').forEach(b => {
    const act = b.dataset.act;
    const disabled = isVideo && act !== 'dl';
    b.classList.toggle('disabled', disabled);
  });
  ctxMenu.classList.add('on');
  const r = ctxMenu.getBoundingClientRect();
  ctxMenu.style.left = Math.min(x, window.innerWidth - r.width - 8) + 'px';
  ctxMenu.style.top = Math.min(y, window.innerHeight - r.height - 8) + 'px';
}
if (ctxMenu) {
  ctxMenu.querySelectorAll('button').forEach(b => {
    b.addEventListener('click', async e => {
      e.stopPropagation();
      const p = ctxPhoto;
      const act = b.dataset.act;
      if (!p) { hideCtxMenu(); return; }
      if (b.classList.contains('disabled')) {
        alert('動画には対応していません');
        hideCtxMenu();
        return;
      }
      hideCtxMenu();
      if (act === 'rot-l' || act === 'rot-r') await rotateThumbPhoto(p, act === 'rot-l' ? 'left' : 'right');
      else if (act === 'edit') openEditorForPhoto(p);
      else if (act === 'dl') await downloadUrl(p.original, p.filename);
    });
  });
}
document.addEventListener('click', e => {
  if (ctxMenu && ctxMenu.classList.contains('on') && !ctxMenu.contains(e.target)) hideCtxMenu();
});
document.addEventListener('keydown', e => { if (e.key === 'Escape') hideCtxMenu(); });
window.addEventListener('scroll', () => hideCtxMenu(), { passive: true });
async function rotateThumbPhoto(p, dir) {
  if (p.isVideo) { alert('動画の回転には対応していません'); return; }
  if (!confirm(`「${p.filename}」を${dir === 'left' ? '左' : '右'}に90度回転しますか？`)) return;
  let j = null;
  try {
    const r = await fetch('/api/rotate', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ path: p.path, dir }),
    });
    j = await r.json();
  } catch (err) {
    alert('回転に失敗しました（通信エラー）');
    return;
  }
  if (!j || !j.ok) {
    alert('回転に失敗しました: ' + ((j && j.error) || 'unknown error'));
    return;
  }
  // 一覧の表示を最新化する（サムネイル・縦横比・キャッシュバスター）
  if (j.width && j.height) { p.width = j.width; p.height = j.height; }
  if (j.size) p.size = j.size;
  if (j.thumb) p.thumb = j.thumb;
  if (j.view) p.view = j.view;
  if (j.original) p.original = j.original;
  if (j.filename) p.filename = j.filename;
  render();
}
function openEditorForPhoto(p) {
  if (p.isVideo) { alert('動画の編集には対応していません'); return; }
  const idx = state.photos.findIndex(x => x.path === p.path);
  if (idx < 0) return;
  lbIndex = idx;
  showLb();
  openEditor();
}

const nowViewing = document.getElementById('now-viewing');
let nvTimer = null;

let scrubberItems = [];  // {ym, label, el}

function buildScrubber() {
  const sc = document.getElementById('scrubber');
  sc.innerHTML = '';
  scrubberItems = [];
  const seen = new Set();
  const heads = [...document.querySelectorAll('.month-head')];
  if (!heads.length) { sc.style.display = 'none'; return; }
  sc.style.display = 'flex';
  for (const h of heads) {
    const ym = h.dataset.ym;
    const year = ym.slice(0, 4);
    const mon = parseInt(ym.slice(4, 6), 10);
    const isFirstOfYear = !seen.has(year);
    seen.add(year);
    const row = document.createElement('div');
    row.className = 'mo' + (isFirstOfYear ? ' hasyear' : '');
    const lbl = document.createElement('span');
    lbl.className = 'lbl';
    lbl.textContent = `${year}/${String(mon).padStart(2, '0')}`;
    const bar = document.createElement('div');
    bar.className = 'bar';
    row.appendChild(lbl); row.appendChild(bar);
    row.addEventListener('click', () => h.scrollIntoView({ behavior: 'smooth', block: 'start' }));
    sc.appendChild(row);
    scrubberItems.push({ ym, year, el: row, head: h });
  }
}

// ---------------- year/month scrubber (right edge) ----------------

window.addEventListener('scroll', () => {
  // 現在位置の月を scrubber に反映
  let current = null;
  for (const it of scrubberItems) {
    const r = it.head.getBoundingClientRect();
    if (r.top <= 120) current = it;
    else break;
  }
  scrubberItems.forEach(it => it.el.classList.toggle('now', it === current));
  if (current) {
    const d = new Date(`${current.ym.slice(0,4)}-${current.ym.slice(4,6)}-01T00:00:00`);
    nowViewing.innerHTML =
      `${d.getFullYear()}年${d.getMonth()+1}月<br><span class="nv-sub">photo/${d.getFullYear()}/${current.ym}/</span>`;
    nowViewing.style.display = 'block';
    clearTimeout(nvTimer);
    nvTimer = setTimeout(() => { nowViewing.style.display = 'none'; }, 1600);
  }
  if (window.innerHeight + window.scrollY >= document.body.offsetHeight - 800) loadPhotos();
}, { passive: true });

const lazyObserver = new IntersectionObserver((entries, obs) => {
  for (const e of entries) {
    if (e.isIntersecting) {
      const img = e.target;
      img.src = img.dataset.src;
      img.onload = () => img.classList.add('loaded');
      obs.unobserve(img);
    }
  }
}, { rootMargin: '600px' });

function reload() {
  state.gen++; // 旧 fetch の結果は破棄される
  state.loading = false;
  state.photos = []; state.offset = 0; state.done = false;
  render(); loadPhotos();
}

// ---------------- lightbox ----------------
let lbIndex = -1;
const lb = document.getElementById('lightbox');
const lbContent = document.getElementById('lb-content');
// ビューア・編集画面のどちらかが開いている間は背面スクロールを止める。
// （編集画面はビューアの上に重ねて開くため、両方見て判定する）
function syncBodyScroll() {
  const modal = lb.classList.contains('open')
    || document.getElementById('editor').classList.contains('open');
  document.body.classList.toggle('modal-open', modal);
}
function openLb(p) {
  lbIndex = state.photos.findIndex(x => x.path === p.path);
  showLb();
}
function showLb() {
  const p = state.photos[lbIndex];
  if (!p) return;
  lb.classList.add('open');
  syncBodyScroll();
  lbContent.innerHTML = '';
  lbZoomIdx = LB_ZOOM_FIT; lbBaseW = 0;
  applyLbZoomButtons();
  let el;
  if (p.isVideo) {
    el = document.createElement('video');
    el.controls = true; el.autoplay = true;
    el.src = p.original;
  } else {
    el = document.createElement('img');
    el.className = 'fit';
    // まず軽量プレビュー（長辺1280px）を表示する。拡大操作でオリジナルに切替。
    // 編集・コピー・ダウンロードは従来どおり p.original を使う。
    el.dataset.preview = p.view || '';
    el.dataset.full = p.original;
    el.dataset.isFull = (p.view && p.view !== p.original) ? '0' : '1';
    el.onload = () => applyLbZoom();
    el.onerror = () => {
      // プレビュー生成失敗時（HEIC等）はオリジナルにフォールバック
      if (el.dataset.isFull === '0') {
        el.dataset.isFull = '1';
        el.src = el.dataset.full;
      }
    };
    el.ondblclick = () => toggleLbZoom();
    el.src = el.dataset.isFull === '0' ? el.dataset.preview : el.dataset.full;
  }
  lbContent.appendChild(el);
  document.getElementById('lb-title').textContent =
    `${p.filename}　${p.camera || ''} ${p.width||''}×${p.height||''}`;
  document.getElementById('lb-edit').style.display = p.isVideo ? 'none' : 'block';
  document.getElementById('lb-copy').style.display = p.isVideo ? 'none' : 'block';
  document.getElementById('lb-zoom-in').style.display = p.isVideo ? 'none' : 'block';
  document.getElementById('lb-zoom-out').style.display = p.isVideo ? 'none' : 'block';
  document.getElementById('lb-zoom-label').style.display = p.isVideo ? 'none' : 'block';
  history.replaceState(null, '', '#p=' + encodeURIComponent(p.path));
}
// ---------------- viewer zoom ----------------
// デフォルトは画面内に収まる縮小表示(fit)。拡大・縮小ボタンで段階ズームし、
// はみ出した分はスクロールで見られる。写真を替えると fit に戻る。
const LB_ZOOM_STEPS = [0.25, 0.5, 0.75, 1, 1.5, 2, 3, 4];
const LB_ZOOM_FIT = 3;
let lbZoomIdx = LB_ZOOM_FIT, lbBaseW = 0;
function applyLbZoomButtons() {
  const zin = document.getElementById('lb-zoom-in');
  const zout = document.getElementById('lb-zoom-out');
  const zlab = document.getElementById('lb-zoom-label');
  if (zin) zin.disabled = lbZoomIdx >= LB_ZOOM_STEPS.length - 1;
  if (zout) zout.disabled = lbZoomIdx <= 0;
  if (zlab) zlab.textContent = Math.round(LB_ZOOM_STEPS[lbZoomIdx] * 100) + '%';
}
function applyLbZoom() {
  const img = lbContent.querySelector('img');
  if (!img || !img.naturalWidth) { applyLbZoomButtons(); return; }
  const f = LB_ZOOM_STEPS[lbZoomIdx];
  if (f === 1) {
    img.classList.add('fit'); img.classList.remove('zoomed');
    img.style.width = ''; img.style.height = '';
  } else {
    if (!lbBaseW) lbBaseW = img.clientWidth || img.naturalWidth;
    img.classList.remove('fit'); img.classList.add('zoomed');
    img.style.width = Math.max(1, Math.round(lbBaseW * f)) + 'px';
    img.style.height = 'auto';
  }
  applyLbZoomButtons();
}
function stepLbZoom(dir) {
  if (!lbContent.querySelector('img')) return;
  const next = Math.min(LB_ZOOM_STEPS.length - 1, Math.max(0, lbZoomIdx + dir));
  if (next === lbZoomIdx) return;
  lbZoomIdx = next;
  applyLbZoom();
}
function toggleLbZoom() {
  if (!lbContent.querySelector('img')) return;
  const toZoom = (lbZoomIdx === LB_ZOOM_FIT);
  lbZoomIdx = toZoom ? LB_ZOOM_FIT + 2 : LB_ZOOM_FIT;
  if (toZoom) upgradeLbToOriginal();
  applyLbZoom();
}
// プレビュー表示中ならオリジナル画像に切り替える（拡大時の高画質化）。
// lbBaseW（ズーム基準幅）は維持する。切り替え後の onload → applyLbZoom で
// 同じ基準幅から再計算されるため、表示サイズが跳ねない。
function upgradeLbToOriginal() {
  const img = lbContent.querySelector('img');
  if (!img || img.dataset.isFull === '1') return false;
  img.dataset.isFull = '1';
  flashLbTitle('高画質読み込み中…');
  img.src = img.dataset.full;
  return true;
}
document.getElementById('lb-zoom-in').onclick = (e) => { e.stopPropagation(); stepLbZoom(1); upgradeLbToOriginal(); };
document.getElementById('lb-zoom-out').onclick = (e) => { e.stopPropagation(); stepLbZoom(-1); };
document.getElementById('lb-zoom-label').onclick = (e) => {
  e.stopPropagation();
  if (!lbContent.querySelector('img')) return;
  lbZoomIdx = LB_ZOOM_FIT; lbBaseW = 0;
  applyLbZoom();
};
function moveLb(delta) {
  if (lbIndex < 0) return;
  lbIndex = (lbIndex + delta + state.photos.length) % state.photos.length;
  showLb();
}
document.getElementById('lb-close').onclick = () => { lb.classList.remove('open'); lbContent.innerHTML = ''; syncBodyScroll(); };
document.getElementById('lb-dl').onclick = async () => {
  const p = state.photos[lbIndex];
  if (!p) return;
  await downloadUrl(p.original, p.filename);
};
document.getElementById('lb-newtab').onclick = (e) => {
  e.stopPropagation();
  const p = state.photos[lbIndex];
  if (!p) return;
  window.open(p.original, '_blank', 'noopener');
};
document.getElementById('lb-del').onclick = async () => {
  const p = state.photos[lbIndex];
  if (!p) return;
  if (!confirm(`「${p.filename}」を削除しますか？\n（一覧・ファイル実体・サムネイルから削除されます。元に戻せません）`)) return;
  let j = null;
  try {
    const r = await fetch('/api/delete', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ paths: [p.path] }),
    });
    j = await r.json();
  } catch (err) {
    alert('削除に失敗しました（通信エラー）');
    return;
  }
  if (!j || !j.ok || !(j.deleted || []).includes(p.path)) {
    alert('削除に失敗しました: ' + ((j && j.error) || 'unknown error'));
    return;
  }
  state.photos.splice(lbIndex, 1);
  state.selected.delete(p.path);
  // 写真が残っていないフォルダ・月の明示チェックは外す
  const seg = p.path.split('/');
  const folder = seg.length >= 3 ? seg.slice(0, 3).join('/') : '';
  const monthPrefix = seg.length >= 2 ? seg.slice(0, 2).join('/') + '/' : '';
  if (folder && !state.photos.some(x => x.path.startsWith(folder + '/'))) state.folderSel.delete(folder);
  if (monthPrefix && !state.photos.some(x => x.path.startsWith(monthPrefix))) state.monthSel.delete(monthPrefix);
  if (!state.photos.length) {
    document.getElementById('lb-close').click();
  } else {
    if (lbIndex >= state.photos.length) lbIndex = state.photos.length - 1;
    showLb();
  }
  render();
  loadMonths();
};
document.getElementById('prev').onclick = () => moveLb(-1);
document.getElementById('next').onclick = () => moveLb(1);
document.getElementById('lb-prev').onclick = (e) => { e.stopPropagation(); moveLb(-1); };
document.getElementById('lb-next').onclick = (e) => { e.stopPropagation(); moveLb(1); };
document.addEventListener('keydown', e => {
  if (!lb.classList.contains('open')) return;
  if (e.key === 'Escape') document.getElementById('lb-close').click();
  if (e.key === 'ArrowLeft') moveLb(-1);
  if (e.key === 'ArrowRight') moveLb(1);
  if (e.key === '+' || e.key === '=') stepLbZoom(1);
  if (e.key === '-') stepLbZoom(-1);
});
lb.addEventListener('click', e => { if (e.target === lb) document.getElementById('lb-close').click(); });

// ---------------- editor ----------------
// 画像編集（トリミング / モザイク / ぼかし / リサイズ）。Canvas で加工し、
// 上書き・別名・編集フォルダのいずれかで保存する。動画は対象外。
const edCanvas = document.getElementById('ed-canvas');
const edCtx = edCanvas.getContext('2d', { willReadFrequently: true });
const edWrap = document.getElementById('ed-wrap');
const edCropBox = document.getElementById('ed-cropbox');
const ed = { tool: null, path: '', filename: '', dirty: false, cropRect: null, cropDrag: null,
  painting: false, lastPt: null, shapeDrag: null, dragSnap: null,
  brushSizes: [12, 24, 48, 96, 192],
  mosaicBlocks: [4, 8, 16, 32, 64],
  blurRadii: [2, 5, 10, 20, 40],
  lineWidths: [4, 8, 16, 32, 64] };

function openEditor() {
  const p = state.photos[lbIndex];
  if (!p) return;
  if (p.isVideo) { alert('動画の編集には対応していません'); return; }
  ed.path = p.path; ed.filename = p.filename;
  ed.tool = null; ed.dirty = false; ed.cropRect = null; ed.cropDrag = null;
  ed.shapeDrag = null; ed.dragSnap = null;
  ed.history = []; updateUndoButton();
  document.querySelectorAll('#editor .ed-side > button[data-tool]').forEach(x => x.classList.remove('on'));
  document.querySelectorAll('#editor .ed-panel').forEach(x => x.classList.remove('on'));
  edCropBox.style.display = 'none';
  document.getElementById('ed-title').textContent = '編集中: ' + p.filename;
  const img = new Image();
  img.onload = () => {
    edCanvas.width = img.naturalWidth; edCanvas.height = img.naturalHeight;
    edCtx.drawImage(img, 0, 0);
    updateResizeInfo();
    document.getElementById('editor').classList.add('open');
    syncBodyScroll();
  };
  img.onerror = () => alert('画像を読み込めませんでした');
  img.src = p.original + (p.original.includes('?') ? '&' : '?') + 't=' + Date.now();
}
function closeEditor(force) {
  if (!force && ed.dirty && !confirm('編集内容を破棄して閉じますか？')) return;
  document.getElementById('editor').classList.remove('open');
  syncBodyScroll();
  ed.tool = null; ed.dirty = false; ed.cropRect = null;
}
document.getElementById('lb-edit').onclick = () => openEditor();
document.getElementById('lb-copy').onclick = () => {
  const p = state.photos[lbIndex];
  if (!p || p.isVideo) return;
  if (typeof ClipboardItem === 'undefined' || !navigator.clipboard?.write) {
    alert('このブラウザは画像のコピーに対応していません');
    return;
  }
  // Blob → PNG 変換（maxSide > 0 で長辺を指定pxに縮小。iPhone の巨大画像対策）
  const toPng = (blob, maxSide) => new Promise((res, rej) => {
    const img = new Image();
    img.onload = () => {
      try {
        let w = img.naturalWidth, h = img.naturalHeight;
        const m = Math.max(w, h);
        if (maxSide > 0 && m > maxSide) {
          const s = maxSide / m;
          w = Math.round(w * s); h = Math.round(h * s);
        }
        const c = document.createElement('canvas');
        c.width = w; c.height = h;
        c.getContext('2d').drawImage(img, 0, 0, w, h);
        c.toBlob(b => b ? res(b) : rej(new Error('encode failed')), 'image/png');
      } catch (e) { rej(e); }
    };
    img.onerror = rej;
    img.src = URL.createObjectURL(blob);
  });
  const writeBlob = (blob) => navigator.clipboard.write(
    [new ClipboardItem({ [blob.type || 'image/png']: blob })]);
  // iOS Safari は PNG のみ対応のため最初から PNG 化する（長辺2048に縮小）。
  // gesture 内で write を呼ぶ Safari 対策として Promise を直接渡す。
  if (/iPad|iPhone|iPod/.test(navigator.userAgent || '')) {
    const pngP = (async () => {
      const r = await fetch(p.original, { cache: 'force-cache' });
      return toPng(await r.blob(), 2048);
    })();
    navigator.clipboard.write([new ClipboardItem({ 'image/png': pngP })]).then(
      () => flashLbTitle('コピーしました'),
      () => alert('コピーに失敗しました'));
    return;
  }
  (async () => {
    try {
      const r = await fetch(p.original, { cache: 'force-cache' });
      const blob = await r.blob();
      try {
        await writeBlob(blob);
      } catch (err) {
        // 元形式が非対応の場合は PNG に変換して再試行
        await writeBlob(await toPng(blob, 0));
      }
      flashLbTitle('コピーしました');
    } catch (err) {
      alert('コピーに失敗しました');
    }
  })();
};
function flashLbTitle(msg) {
  const t = document.getElementById('lb-title');
  const orig = t.textContent;
  t.textContent = msg;
  setTimeout(() => { t.textContent = orig; }, 1500);
}
document.getElementById('ed-close').onclick = () => closeEditor(false);

// ---------------- rotate ----------------
// 押すたびにキャンバスごと90度回転する（左回転=反時計回り、右回転=時計回り）。
// 保存（上書き・別名・編集フォルダ）には回転後の内容が使われる。
function rotateEdCanvas(dir) {
  const w = edCanvas.width, h = edCanvas.height;
  if (!w || !h) return;
  edPushHistory();
  const c = document.createElement('canvas');
  c.width = h; c.height = w;
  const ctx = c.getContext('2d');
  ctx.translate(c.width / 2, c.height / 2);
  ctx.rotate(dir * Math.PI / 2);
  ctx.drawImage(edCanvas, -w / 2, -h / 2);
  edCanvas.width = h; edCanvas.height = w;
  edCtx.drawImage(c, 0, 0);
  ed.cropRect = null; edCropBox.style.display = 'none';
  cancelShape(false);
  ed.dirty = true; updateResizeInfo();
}
document.getElementById('ed-rot-l').onclick = () => rotateEdCanvas(-1);
document.getElementById('ed-rot-r').onclick = () => rotateEdCanvas(1);

// ---------------- editor zoom（表示のみ。画像データは変えない） ----------------
// デフォルトは画面内に収まる縮小表示(fit)。拡大・縮小ボタンで段階ズームし、
// はみ出した分はスクロールで見られる。回転・切抜き・リサイズ・1つ戻す・
// 開き直しで fit に戻る。座標換算は表示倍率で行うため編集操作に影響しない。
const ED_ZOOM_STEPS = [0.25, 0.5, 0.75, 1, 1.5, 2, 3, 4];
const ED_ZOOM_FIT = 3;
let edZoomIdx = ED_ZOOM_FIT, edBaseW = 0;
function applyEdZoomButtons() {
  const zin = document.getElementById('ed-zoom-in');
  const zout = document.getElementById('ed-zoom-out');
  const zlab = document.getElementById('ed-zoom-label');
  if (zin) zin.disabled = edZoomIdx >= ED_ZOOM_STEPS.length - 1;
  if (zout) zout.disabled = edZoomIdx <= 0;
  if (zlab) zlab.textContent = Math.round(ED_ZOOM_STEPS[edZoomIdx] * 100) + '%';
}
function applyEdZoom() {
  const f = ED_ZOOM_STEPS[edZoomIdx];
  if (f === 1) {
    edCanvas.style.width = ''; edCanvas.style.height = '';
    edWrap.classList.remove('zoomed');
  } else {
    if (!edBaseW) edBaseW = edCanvas.clientWidth || edCanvas.width;
    edCanvas.style.width = Math.max(1, Math.round(edBaseW * f)) + 'px';
    edCanvas.style.height = 'auto';
    edWrap.classList.add('zoomed');
  }
  // 選択中のトリミング枠があれば新しい表示倍率で置き直す
  if (ed.cropRect && edCropBox.style.display === 'block') placeCropBox(ed.cropRect);
  updateBrushCursor();
  applyEdZoomButtons();
}
function stepEdZoom(dir) {
  const next = Math.min(ED_ZOOM_STEPS.length - 1, Math.max(0, edZoomIdx + dir));
  if (next === edZoomIdx) return;
  edZoomIdx = next;
  applyEdZoom();
}
function resetEdZoom() {
  edZoomIdx = ED_ZOOM_FIT; edBaseW = 0;
  edCanvas.style.width = ''; edCanvas.style.height = '';
  edWrap.classList.remove('zoomed');
  applyEdZoomButtons();
}
document.getElementById('ed-zoom-in').onclick = (e) => { e.stopPropagation(); stepEdZoom(1); };
document.getElementById('ed-zoom-out').onclick = (e) => { e.stopPropagation(); stepEdZoom(-1); };
document.getElementById('ed-zoom-label').onclick = (e) => {
  e.stopPropagation();
  edZoomIdx = ED_ZOOM_FIT; edBaseW = 0;
  applyEdZoom();
};

// ---------------- undo（1つ戻す） ----------------
// 破壊的操作の直前にキャンバス全体をスナップショットとして保持する
// （保存時と同じ形式・品質。JPEG/WebP は 0.92、PNG はロスレス。
// モザイク等の筆操作は1ストローク=1履歴）。
const ED_HISTORY_MAX = 20;
function edPushHistory() {
  try {
    ed.history.push(edHistoryURL());
    while (ed.history.length > ED_HISTORY_MAX) ed.history.shift();
  } catch (err) { /* 巨大画像等で取れなければ履歴なしで続行 */ }
  updateUndoButton();
}
function edHistoryURL() {
  // PNG 保存時は透過情報を落とさないよう PNG で保持する
  if (edSaveFormat().mime === 'image/png') return edCanvas.toDataURL('image/png');
  return edCanvas.toDataURL('image/jpeg', 0.92);
}
function updateUndoButton() {
  const b = document.getElementById('ed-undo');
  if (b) b.disabled = !(ed.history && ed.history.length);
}
document.getElementById('ed-undo').onclick = () => {
  const url = ed.history && ed.history.pop();
  updateUndoButton();
  if (!url) return;
  const img = new Image();
  img.onload = () => {
    edCanvas.width = img.naturalWidth; edCanvas.height = img.naturalHeight;
    edCtx.drawImage(img, 0, 0);
    ed.cropRect = null; edCropBox.style.display = 'none';
    cancelShape(false);
    ed.dirty = true; updateResizeInfo();
  };
  img.onerror = () => alert('1つ戻せませんでした');
  img.src = url;
};
// ---------------- rename（拡張子より前を変更） ----------------
document.getElementById('ed-rename').onclick = async () => {
  const cur = ed.filename || '';
  const dot = cur.lastIndexOf('.');
  const stem = dot > 0 ? cur.slice(0, dot) : cur;
  const ext = dot > 0 ? cur.slice(dot) : '';
  const next = prompt(`新しいファイル名（拡張子 ${ext} はそのまま）`, stem);
  if (next === null) return;
  if (!next.trim() || next.trim() === stem) return;
  let j = null;
  try {
    const r = await fetch('/api/rename', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ path: ed.path, name: next.trim() }),
    });
    j = await r.json();
  } catch (err) {
    alert('変更に失敗しました（通信エラー）');
    return;
  }
  if (!j || !j.ok) {
    alert('変更に失敗しました: ' + ((j && j.error) || 'unknown error'));
    return;
  }
  ed.path = j.path; ed.filename = j.filename;
  document.getElementById('ed-title').textContent = '編集中: ' + j.filename;
  // 背後の一覧・ビューア表示も追従させる
  const p = state.photos[lbIndex];
  if (p && p.path !== j.path) {
    const q = s => (s.includes('?') ? s.slice(s.indexOf('?')) : '');
    if (j.path.startsWith('edit/')) {
      const nm = j.path.slice(5);
      p.original = '/editphoto/' + nm + q(p.original);
      if (p.thumb) p.thumb = '/editthumb/' + nm + q(p.thumb);
    } else {
      p.original = '/photo/' + j.path + q(p.original);
      if (p.thumb) {
        p.thumb = '/thumb/' + j.path.replace(/\.[^.]*$/, '') + '_thumb.webp' + q(p.thumb);
      }
      if (p.view) {
        p.view = '/view/' + j.path.replace(/\.[^.]*$/, '') + '_view.webp' + q(p.view);
      }
    }
    p.path = j.path; p.filename = j.filename;
  }
};

document.querySelectorAll('#editor .ed-side > button[data-tool]').forEach(b => {
  b.onclick = () => {
    ed.tool = (ed.tool === b.dataset.tool) ? null : b.dataset.tool;
    document.querySelectorAll('#editor .ed-side > button[data-tool]')
      .forEach(x => x.classList.toggle('on', x.dataset.tool === ed.tool));
    document.querySelectorAll('#editor .ed-panel')
      .forEach(x => x.classList.toggle('on', x.id === 'ed-panel-' + ed.tool));
    edCropBox.style.display = 'none'; ed.cropRect = null;
    cancelShape(true);
    updateBrushCursor();
  };
});
[['ed-mosaic-strength', 'ed-mosaic-strength-v'], ['ed-mosaic-size', 'ed-mosaic-size-v'],
 ['ed-blur-strength', 'ed-blur-strength-v'], ['ed-blur-size', 'ed-blur-size-v'],
  ['ed-rect-width', 'ed-rect-width-v'], ['ed-arrow-width', 'ed-arrow-width-v'],
  ['ed-pen-width', 'ed-pen-width-v']].forEach(([a, b]) => {
  document.getElementById(a).addEventListener('input', e => {
    document.getElementById(b).textContent = e.target.value;
    updateBrushCursor();
  });
});
window.addEventListener('resize', updateBrushCursor);

// ---------------- brush cursor ----------------
// モザイク・ぼかし選択中は、写真上のカーソルを「太さ」と同じ直径の円にする
// （表示倍率で換算。回転・リサイズ等で表示サイズが変わっても追従する）。
function brushDiameter() {
  const el = document.getElementById(ed.tool === 'mosaic' ? 'ed-mosaic-size' : 'ed-blur-size');
  const idx = Math.max(0, Math.min(4, (parseInt(el && el.value, 10) || 3) - 1));
  return ed.brushSizes[idx];
}
function updateBrushCursor() {
  if ((ed.tool !== 'mosaic' && ed.tool !== 'blur') || !edCanvas.width) {
    edCanvas.style.cursor = '';
    return;
  }
  const r = edCanvas.getBoundingClientRect();
  const d = Math.max(4, Math.round(brushDiameter() * r.width / edCanvas.width));
  const c = Math.min(d, 128); // ブラウザのカーソル画像の上限
  const h = Math.floor(c / 2);
  const svg = `<svg xmlns='http://www.w3.org/2000/svg' width='${c}' height='${c}'>`
    + `<circle cx='${h}' cy='${h}' r='${h - 1.5}' fill='none' stroke='black' stroke-width='2.5'/>`
    + `<circle cx='${h}' cy='${h}' r='${h - 1.5}' fill='none' stroke='white' stroke-width='1'/></svg>`;
  edCanvas.style.cursor = `url("data:image/svg+xml,${encodeURIComponent(svg)}") ${h} ${h}, crosshair`;
}

// canvas 上の座標を画像ピクセル座標に変換する
function edPos(e) {
  const r = edCanvas.getBoundingClientRect();
  return { x: (e.clientX - r.left) * edCanvas.width / r.width,
           y: (e.clientY - r.top) * edCanvas.height / r.height };
}

// ---------------- crop ----------------
function cropRectOf() {
  const d = ed.cropDrag;
  if (!d) return null;
  let x0 = Math.min(d.x0, d.x1), y0 = Math.min(d.y0, d.y1);
  let w = Math.abs(d.x1 - d.x0), h = Math.abs(d.y1 - d.y0);
  if (document.querySelector('input[name="ed-ratio"]:checked').value === 'keep') {
    const a = edCanvas.width / edCanvas.height;
    h = w / a;
    y0 = (d.y1 < d.y0) ? d.y0 - h : d.y0;
    x0 = (d.x1 < d.x0) ? d.x0 - w : d.x0;
  }
  x0 = Math.max(0, Math.min(edCanvas.width - 1, x0));
  y0 = Math.max(0, Math.min(edCanvas.height - 1, y0));
  w = Math.max(1, Math.min(w, edCanvas.width - x0));
  h = Math.max(1, Math.min(h, edCanvas.height - y0));
  return { x: Math.round(x0), y: Math.round(y0), w: Math.round(w), h: Math.round(h) };
}
function placeCropBox(r) {
  const cr = edCanvas.getBoundingClientRect(), wr = edWrap.getBoundingClientRect();
  const sx = cr.width / edCanvas.width, sy = cr.height / edCanvas.height;
  edCropBox.style.display = 'block';
  edCropBox.style.left = (cr.left - wr.left + r.x * sx) + 'px';
  edCropBox.style.top = (cr.top - wr.top + r.y * sy) + 'px';
  edCropBox.style.width = (r.w * sx) + 'px';
  edCropBox.style.height = (r.h * sy) + 'px';
}
function drawCropBox() {
  const r = cropRectOf();
  if (!r) { edCropBox.style.display = 'none'; ed.cropRect = null; return; }
  ed.cropRect = r;
  placeCropBox(r);
}
document.getElementById('ed-crop-apply').onclick = () => {
  const r = ed.cropRect;
  if (!r || r.w < 2 || r.h < 2) { alert('範囲を選択してください'); return; }
  edPushHistory();
  const c = document.createElement('canvas');
  c.width = r.w; c.height = r.h;
  c.getContext('2d').drawImage(edCanvas, r.x, r.y, r.w, r.h, 0, 0, r.w, r.h);
  edCanvas.width = r.w; edCanvas.height = r.h;
  edCtx.drawImage(c, 0, 0);
  ed.cropRect = null; edCropBox.style.display = 'none';
  ed.dirty = true; updateResizeInfo();
};
document.getElementById('ed-crop-clear').onclick = () => {
  ed.cropDrag = null; ed.cropRect = null; edCropBox.style.display = 'none';
};

// ---------------- mosaic / blur brush ----------------
// モザイクは画像原点に固定したグリッドで区切り、1ストローク中に塗った
// ブロックは再計算しない。ドラッグで重ね塗りしても、既にモザイク化された
// 画素をさらに平均化しないため、ブロック状が保たれる（ぼかし状にならない）。
function paintMosaic(cx, cy, radius, block) {
  const B = Math.max(1, Math.round(block));
  if (!ed.mosaicDone) ed.mosaicDone = new Set();
  // 作業範囲はブロック境界に合わせる（中途半端な塗りを残さない）
  const x0 = Math.max(0, Math.floor((cx - radius) / B) * B);
  const y0 = Math.max(0, Math.floor((cy - radius) / B) * B);
  const x1 = Math.min(edCanvas.width, Math.ceil((cx + radius) / B) * B);
  const y1 = Math.min(edCanvas.height, Math.ceil((cy + radius) / B) * B);
  const W = x1 - x0, H = y1 - y0;
  if (W <= 0 || H <= 0) return;
  const img = edCtx.getImageData(x0, y0, W, H);
  const d = img.data;
  const fills = [];
  const gx0 = x0 / B, gy0 = y0 / B;
  const gx1 = Math.floor((x1 - 1) / B), gy1 = Math.floor((y1 - 1) / B);
  for (let gy = gy0; gy <= gy1; gy++) {
    for (let gx = gx0; gx <= gx1; gx++) {
      const key = gx + ',' + gy;
      if (ed.mosaicDone.has(key)) continue;
      const bcx = gx * B + B / 2, bcy = gy * B + B / 2;
      const dx = bcx - cx, dy = bcy - cy;
      if (dx * dx + dy * dy > radius * radius) continue;
      const px0 = gx * B, py0 = gy * B;
      const px1 = Math.min(px0 + B, x1), py1 = Math.min(py0 + B, y1);
      let r = 0, g = 0, b = 0, a = 0, n = 0;
      for (let y = py0; y < py1; y++) {
        const row = (y - y0) * W;
        for (let x = px0; x < px1; x++) {
          const i = (row + (x - x0)) * 4;
          r += d[i]; g += d[i + 1]; b += d[i + 2]; a += d[i + 3]; n++;
        }
      }
      if (!n) continue;
      ed.mosaicDone.add(key);
      fills.push({ x: px0, y: py0, w: px1 - px0, h: py1 - py0,
                   c: `rgba(${Math.round(r / n)},${Math.round(g / n)},${Math.round(b / n)},${(a / n / 255).toFixed(3)})` });
    }
  }
  for (const f of fills) {
    edCtx.fillStyle = f.c;
    edCtx.fillRect(f.x, f.y, f.w, f.h);
  }
}
function paintBlur(cx, cy, radius, rad) {
  const x0 = Math.max(0, Math.floor(cx - radius - rad)), y0 = Math.max(0, Math.floor(cy - radius - rad));
  const x1 = Math.min(edCanvas.width, Math.ceil(cx + radius + rad)), y1 = Math.min(edCanvas.height, Math.ceil(cy + radius + rad));
  const W = x1 - x0, H = y1 - y0;
  if (W <= 0 || H <= 0) return;
  const src = edCtx.getImageData(x0, y0, W, H);
  const w = W, h = H, win = rad * 2 + 1;
  const sp = new Uint8ClampedArray(src.data);
  const tmp = new Float32Array(w * h * 4);
  for (let y = 0; y < h; y++) {  // 水平パス
    let r = 0, g = 0, b = 0, a = 0;
    for (let x = -rad; x <= rad; x++) {
      const i = (y * w + Math.min(w - 1, Math.max(0, x))) * 4;
      r += sp[i]; g += sp[i + 1]; b += sp[i + 2]; a += sp[i + 3];
    }
    for (let x = 0; x < w; x++) {
      const o = (y * w + x) * 4;
      tmp[o] = r / win; tmp[o + 1] = g / win; tmp[o + 2] = b / win; tmp[o + 3] = a / win;
      const io = (y * w + Math.min(w - 1, Math.max(0, x - rad))) * 4;
      const ia = (y * w + Math.min(w - 1, Math.max(0, x + rad + 1))) * 4;
      r += sp[ia] - sp[io]; g += sp[ia + 1] - sp[io + 1];
      b += sp[ia + 2] - sp[io + 2]; a += sp[ia + 3] - sp[io + 3];
    }
  }
  const out = src.data;
  for (let x = 0; x < w; x++) {  // 垂直パス（円内のみ書き戻し）
    let r = 0, g = 0, b = 0, a = 0;
    for (let y = -rad; y <= rad; y++) {
      const i = (Math.min(h - 1, Math.max(0, y)) * w + x) * 4;
      r += tmp[i]; g += tmp[i + 1]; b += tmp[i + 2]; a += tmp[i + 3];
    }
    for (let y = 0; y < h; y++) {
      const dx = x0 + x - cx, dy = y0 + y - cy;
      if (dx * dx + dy * dy <= radius * radius) {
        const o = (y * w + x) * 4;
        out[o] = r / win; out[o + 1] = g / win; out[o + 2] = b / win; out[o + 3] = a / win;
      }
      const io = (Math.min(h - 1, Math.max(0, y - rad)) * w + x) * 4;
      const ia = (Math.min(h - 1, Math.max(0, y + rad + 1)) * w + x) * 4;
      r += tmp[ia] - tmp[io]; g += tmp[ia + 1] - tmp[io + 1];
      b += tmp[ia + 2] - tmp[io + 2]; a += tmp[ia + 3] - tmp[io + 3];
    }
  }
  edCtx.putImageData(src, x0, y0);
}
function paintStroke(x0, y0, x1, y1) {
  const isM = ed.tool === 'mosaic';
  const sizeIdx = parseInt(document.getElementById(isM ? 'ed-mosaic-size' : 'ed-blur-size').value, 10) - 1;
  const strIdx = parseInt(document.getElementById(isM ? 'ed-mosaic-strength' : 'ed-blur-strength').value, 10) - 1;
  const radius = ed.brushSizes[sizeIdx] / 2;
  const dist = Math.hypot(x1 - x0, y1 - y0);
  const steps = Math.max(1, Math.ceil(dist / Math.max(2, radius / 3)));
  for (let i = 0; i <= steps; i++) {
    const x = x0 + (x1 - x0) * i / steps, y = y0 + (y1 - y0) * i / steps;
    if (isM) paintMosaic(x, y, radius, ed.mosaicBlocks[strIdx]);
    else paintBlur(x, y, radius, ed.blurRadii[strIdx]);
  }
  ed.dirty = true;
}
edCanvas.addEventListener('pointerdown', e => {
  if (!ed.tool) return;
  e.preventDefault();
  try { edCanvas.setPointerCapture(e.pointerId); } catch (err) { /* noop */ }
  const pt = edPos(e);
  if (ed.tool === 'crop') {
    ed.cropDrag = { x0: pt.x, y0: pt.y, x1: pt.x, y1: pt.y };
    drawCropBox();
  } else if (ed.tool === 'mosaic' || ed.tool === 'blur') {
    edPushHistory();
    ed.painting = true; ed.lastPt = pt;
    ed.mosaicDone = new Set(); // この1ストロークで塗ったモザイク枡の記録
    paintStroke(pt.x, pt.y, pt.x, pt.y);
  } else if (ed.tool === 'rect' || ed.tool === 'arrow' || ed.tool === 'pen') {
    edPushHistory();
    ed.dragSnap = snapEdCanvas();
    ed.shapeDrag = ed.tool === 'pen'
      ? { pts: [{ x: pt.x, y: pt.y }] }
      : { x0: pt.x, y0: pt.y, x1: pt.x, y1: pt.y };
  }
});

// ---------------- rect / arrow / pen shape ----------------
// ドラッグ中はスナップショットに戻してから図形を描く（プレビュー）。
// 指を離したときに確定する。小さすぎるドラッグは無効として履歴も戻す。
function edLineWidth() {
  const id = ed.tool === 'arrow' ? 'ed-arrow-width'
    : ed.tool === 'pen' ? 'ed-pen-width' : 'ed-rect-width';
  const el = document.getElementById(id);
  const idx = Math.max(0, Math.min(4, (parseInt(el && el.value, 10) || 3) - 1));
  return ed.lineWidths[idx];
}
function snapEdCanvas() {
  const c = document.createElement('canvas');
  c.width = edCanvas.width; c.height = edCanvas.height;
  c.getContext('2d').drawImage(edCanvas, 0, 0);
  return c;
}
function cancelShape(restore) {
  if (restore && ed.shapeDrag && ed.dragSnap) {
    edCtx.drawImage(ed.dragSnap, 0, 0);
    ed.history.pop(); updateUndoButton();
  }
  ed.shapeDrag = null; ed.dragSnap = null;
}
function strokeRoundRectPath(g, x, y, w, h, r) {
  g.beginPath();
  g.moveTo(x + r, y);
  g.lineTo(x + w - r, y);
  g.arcTo(x + w, y, x + w, y + r, r);
  g.lineTo(x + w, y + h - r);
  g.arcTo(x + w, y + h, x + w - r, y + h, r);
  g.lineTo(x + r, y + h);
  g.arcTo(x, y + h, x, y + h - r, r);
  g.lineTo(x, y + r);
  g.arcTo(x, y, x + r, y, r);
  g.closePath();
}
function drawArrowShape(g, x0, y0, x1, y1, lw) {
  const dx = x1 - x0, dy = y1 - y0;
  const len = Math.hypot(dx, dy);
  if (len < 5) return false;
  const ux = dx / len, uy = dy / len;
  const headLen = Math.max(lw * 3.5, 10), headW = Math.max(lw * 2.5, 8);
  const bx = x1 - ux * headLen, by = y1 - uy * headLen;
  g.strokeStyle = '#ff0000'; g.fillStyle = '#ff0000';
  g.lineWidth = lw; g.lineCap = 'round'; g.lineJoin = 'round';
  g.beginPath();
  g.moveTo(x0, y0); g.lineTo(bx, by);
  g.stroke();
  const px = -uy, py = ux;
  g.beginPath();
  g.moveTo(x1, y1);
  g.lineTo(bx + px * headW / 2, by + py * headW / 2);
  g.lineTo(bx - px * headW / 2, by - py * headW / 2);
  g.closePath();
  g.fill();
  return true;
}
function drawPenShape(g, s, lw) {
  const pts = s && s.pts;
  if (!pts || pts.length < 1) return false;
  let len = 0;
  for (let i = 1; i < pts.length; i++) len += Math.hypot(pts[i].x - pts[i - 1].x, pts[i].y - pts[i - 1].y);
  if (len < 5) return false;
  g.strokeStyle = '#ff0000';
  g.lineWidth = lw; g.lineCap = 'round'; g.lineJoin = 'round';
  g.beginPath();
  g.moveTo(pts[0].x, pts[0].y);
  for (let i = 1; i < pts.length; i++) g.lineTo(pts[i].x, pts[i].y);
  g.stroke();
  return true;
}
function drawShape(g, s) {
  const lw = edLineWidth();
  if (ed.tool === 'pen') return drawPenShape(g, s, lw);
  if (ed.tool === 'rect') {
    const x = Math.min(s.x0, s.x1), y = Math.min(s.y0, s.y1);
    const w = Math.abs(s.x1 - s.x0), h = Math.abs(s.y1 - s.y0);
    if (w < 4 || h < 4) return false;
    g.strokeStyle = '#ff0000';
    g.lineWidth = lw; g.lineJoin = 'round';
    strokeRoundRectPath(g, x, y, w, h, Math.max(2, Math.min(w, h) * 0.12));
    g.stroke();
    return true;
  }
  return drawArrowShape(g, s.x0, s.y0, s.x1, s.y1, lw);
}
function previewShape() {
  const s = ed.shapeDrag;
  if (!s || !ed.dragSnap) return;
  edCtx.drawImage(ed.dragSnap, 0, 0);
  drawShape(edCtx, s);
}
edCanvas.addEventListener('pointermove', e => {
  if (!ed.tool) return;
  const pt = edPos(e);
  if (ed.tool === 'crop' && ed.cropDrag) {
    ed.cropDrag.x1 = pt.x; ed.cropDrag.y1 = pt.y;
    drawCropBox();
  } else if (ed.painting && ed.lastPt && (ed.tool === 'mosaic' || ed.tool === 'blur')) {
    paintStroke(ed.lastPt.x, ed.lastPt.y, pt.x, pt.y);
    ed.lastPt = pt;
  } else if (ed.shapeDrag && (ed.tool === 'rect' || ed.tool === 'arrow')) {
    ed.shapeDrag.x1 = pt.x; ed.shapeDrag.y1 = pt.y;
    previewShape();
  } else if (ed.shapeDrag && ed.tool === 'pen') {
    const pts = ed.shapeDrag.pts;
    const last = pts[pts.length - 1];
    // 近すぎる点は間引く（履歴・描画の肥大化防止）
    if (Math.hypot(pt.x - last.x, pt.y - last.y) >= 2) {
      pts.push({ x: pt.x, y: pt.y });
      previewShape();
    }
  }
});
edCanvas.addEventListener('pointerup', () => {
  if (ed.shapeDrag && ed.dragSnap) {
    edCtx.drawImage(ed.dragSnap, 0, 0);
    if (drawShape(edCtx, ed.shapeDrag)) ed.dirty = true;
    else { ed.history.pop(); updateUndoButton(); }
    ed.shapeDrag = null; ed.dragSnap = null;
  }
  ed.cropDrag = null; ed.painting = false; ed.lastPt = null; ed.mosaicDone = null;
});
edCanvas.addEventListener('pointercancel', () => {
  cancelShape(true);
  ed.cropDrag = null; ed.painting = false; ed.lastPt = null; ed.mosaicDone = null;
});

// ---------------- resize ----------------
function updateResizeInfo() {
  document.getElementById('ed-rs-cur').textContent = `現在: ${edCanvas.width}×${edCanvas.height}`;
  // 画像サイズが変わる操作（開く・回転・切抜き・リサイズ・1つ戻す）は
  // 全てここを通るので、表示ズームも fit に戻す。
  resetEdZoom();
  updateBrushCursor();
}
document.querySelectorAll('#ed-panel-resize [data-w]').forEach(b => {
  b.onclick = () => { document.getElementById('ed-rs-w').value = b.dataset.w; };
});
document.getElementById('ed-rs-swap').onclick = () => {
  const wEl = document.getElementById('ed-rs-w');
  const hEl = document.getElementById('ed-rs-h');
  const tmp = wEl.value;
  wEl.value = hEl.value;
  hEl.value = tmp;
};
document.getElementById('ed-rs-apply').onclick = () => {
  const L = parseInt(document.getElementById('ed-rs-long').value, 10) || 0;
  const W = parseInt(document.getElementById('ed-rs-w').value, 10) || 0;
  const H = parseInt(document.getElementById('ed-rs-h').value, 10) || 0;
  const cw = edCanvas.width, ch = edCanvas.height;
  let tw = 0, th = 0;
  if (L > 0) { const s = L / Math.max(cw, ch); tw = Math.round(cw * s); th = Math.round(ch * s); }
  else if (W > 0) { const s = W / cw; tw = W; th = Math.round(ch * s); }
  else if (H > 0) { const s = H / ch; th = H; tw = Math.round(cw * s); }
  else { alert('サイズを入力してください'); return; }
  tw = Math.max(1, Math.min(8192, tw)); th = Math.max(1, Math.min(8192, th));
  if (tw === cw && th === ch) return;
  edPushHistory();
  const c = document.createElement('canvas');
  c.width = tw; c.height = th;
  const g = c.getContext('2d');
  g.imageSmoothingQuality = 'high';
  g.drawImage(edCanvas, 0, 0, tw, th);
  edCanvas.width = tw; edCanvas.height = th;
  edCtx.imageSmoothingQuality = 'high';
  edCtx.drawImage(c, 0, 0);
  ed.dirty = true; updateResizeInfo();
};

// ---------------- editor save ----------------
// 元画像の拡張子から保存形式を決める（canvas が書き出せるものだけ元形式を維持）。
// png→PNG、webp→WebP、jpg/jpeg→JPEG、それ以外（gif/bmp/tiff/heic等）はJPEG。
function edSaveFormat() {
  const n = String(ed.filename || ed.path || '').toLowerCase();
  if (n.endsWith('.png')) return { mime: 'image/png', ext: '.png' };
  if (n.endsWith('.webp')) return { mime: 'image/webp', ext: '.webp' };
  return { mime: 'image/jpeg', ext: '.jpg' };
}
function edEncode() {
  // 希望形式→PNG→JPEG の順に試す（ブラウザが WebP 書き出し不可の場合等に備える）。
  // 実際に出力できた形式を { blob, mime, ext } で返す。
  const want = edSaveFormat().mime;
  const cands = [want, 'image/png', 'image/jpeg'].filter((v, i, a) => a.indexOf(v) === i);
  const extOf = mime => mime === 'image/png' ? '.png' : (mime === 'image/webp' ? '.webp' : '.jpg');
  return new Promise((res, rej) => {
    const tryNext = k => {
      if (k >= cands.length) { rej(new Error('encode failed')); return; }
      const mime = cands[k];
      try {
        edCanvas.toBlob(
          b => { if (b) res({ blob: b, mime, ext: extOf(mime) }); else tryNext(k + 1); },
          mime, mime === 'image/png' ? undefined : 0.92);
      } catch (err) { tryNext(k + 1); }
    };
    tryNext(0);
  });
}
function edStem() {
  const n = ed.filename || 'edit';
  const i = n.lastIndexOf('.');
  return i > 0 ? n.slice(0, i) : n;
}
document.getElementById('ed-overwrite').onclick = async () => {
  if (!confirm(`「${ed.filename}」に上書き保存しますか？（元に戻せません）`)) return;
  const out = await edEncode().catch(() => null);
  if (!out) { alert('画像の書き出しに失敗しました'); return; }
  const fd = new FormData();
  fd.append('path', ed.path);
  fd.append('file', out.blob, 'edit' + out.ext);
  let j = null;
  try {
    const r = await fetch('/api/edit-overwrite', { method: 'POST', body: fd });
    j = await r.json();
  } catch (err) {
    alert('保存に失敗しました（通信エラー）');
    return;
  }
  if (!j || !j.ok) {
    alert('保存に失敗しました: ' + ((j && j.error) || 'unknown error'));
    return;
  }
  closeEditor(true);
  document.getElementById('lb-close').click();
  reload();
};
document.getElementById('ed-saveas').onclick = async () => {
  const out = await edEncode().catch(() => null);
  if (!out) { alert('画像の書き出しに失敗しました'); return; }
  // 元画像と同じフォルダに別名保存する（撮影日時は EXIF で引き継がれるため
  // 並び順は変わらない）。edit/ が元なら編集フォルダに保存される。
  const name = edStem() + '_edit' + out.ext;
  const fd = new FormData();
  fd.append('path', ed.path);
  fd.append('name', name);
  fd.append('file', out.blob, name);
  let j = null;
  try {
    const r = await fetch('/api/edit-saveas', { method: 'POST', body: fd });
    j = await r.json();
  } catch (err) {
    alert('保存に失敗しました（通信エラー）');
    return;
  }
  if (!j || !j.ok) {
    alert('保存に失敗しました: ' + ((j && j.error) || 'unknown error'));
    return;
  }
  closeEditor(true);
  document.getElementById('lb-close').click();
  if (j.path && j.path.startsWith('edit/')) setView('edits');
  else reload();
};
document.getElementById('ed-tolibrary').onclick = async () => {
  const out = await edEncode().catch(() => null);
  if (!out) { alert('画像の書き出しに失敗しました'); return; }
  const name = edStem() + '_edit' + out.ext;
  const fd = new FormData();
  fd.append('name', name);
  fd.append('src', ed.path);
  fd.append('file', out.blob, name);
  let j = null;
  try {
    const r = await fetch('/api/edit-save', { method: 'POST', body: fd });
    j = await r.json();
  } catch (err) {
    alert('保存に失敗しました（通信エラー）');
    return;
  }
  if (!j || !j.ok) {
    alert('保存に失敗しました: ' + ((j && j.error) || 'unknown error'));
    return;
  }
  closeEditor(true);
  document.getElementById('lb-close').click();
  setView('edits');
};

// infinite scroll は上の scroll ハンドラに統合

// ---------------- upload (file picker + drag & drop) ----------------
const fileInput = document.getElementById('file-input');
const dropzone = document.getElementById('dropzone');
// 旧 #up-bar は残置（互換用）。表示は Immich 風の #up-manager が担う。
// ファイル選択はサイドバーの「アップロード」ボタン（nav-upload）が開く。
fileInput.addEventListener('change', () => { uploadFiles([...fileInput.files]); fileInput.value = ''; });

let dragDepth = 0;
window.addEventListener('dragenter', e => {
  e.preventDefault();
  if (++dragDepth === 1) dropzone.classList.add('on');
});
window.addEventListener('dragover', e => e.preventDefault());
window.addEventListener('dragleave', e => {
  e.preventDefault();
  if (--dragDepth <= 0) { dragDepth = 0; dropzone.classList.remove('on'); }
});
window.addEventListener('drop', e => {
  e.preventDefault();
  dragDepth = 0; dropzone.classList.remove('on');
  if (!e.dataTransfer) return;
  const files = [];
  const walk = entry => {
    if (!entry) return;
    if (entry.isFile) {
      entry.file(f => files.push(f));
    } else if (entry.isDirectory) {
      const reader = entry.createReader();
      const readBatch = () => reader.readEntries(entries => {
        if (entries.length) { entries.forEach(walk); readBatch(); }
      });
      readBatch();
    }
  };
  const items = [...(e.dataTransfer.items || [])];
  if (items.length && items[0].webkitGetAsEntry) {
    items.forEach(it => walk(it.webkitGetAsEntry()));
    // entry.file() は非同期なので少し待ってから送る
    setTimeout(() => uploadFiles(files), 300);
  } else {
    uploadFiles([...e.dataTransfer.files]);
  }
});

const UPM_PARALLEL = 3;
const upm = { items: [], running: false, cancelled: false, active: 0,
  startedAt: 0, lastT: 0, lastSent: 0, speed: 0 };
const upmBox = document.getElementById('up-manager');
const upmList = document.getElementById('upm-list');
const upmFill = document.getElementById('upm-fill');
const upmTrack = document.getElementById('upm-track');
const upmSummary = document.getElementById('upm-summary');
const upmCount = document.getElementById('upm-count');
const upmTitle = document.getElementById('upm-title');
const upmRetry = document.getElementById('upm-retry');
function fmtBytes(n) {
  if (!n && n !== 0) return '-';
  if (n < 1024) return `${n} B`;
  const u = ['KB', 'MB', 'GB', 'TB'];
  let v = n / 1024, i = 0;
  while (v >= 1024 && i < u.length - 1) { v /= 1024; i++; }
  return `${v.toFixed(v >= 100 ? 0 : 1)} ${u[i]}`;
}
function fmtSpeed(bps) {
  if (!bps || bps <= 0) return '';
  return fmtBytes(Math.round(bps)) + '/s';
}
function fmtEta(sec) {
  if (!isFinite(sec) || sec < 0) return '';
  if (sec < 60) return `残り約${Math.ceil(sec)}秒`;
  const m = Math.floor(sec / 60), s = Math.ceil(sec % 60);
  return m < 60 ? `残り約${m}分${s}秒` : `残り約${Math.floor(m / 60)}時間${m % 60}分`;
}
function upmRow(item) {
  let row = upmList.querySelector(`[data-id="${item.id}"]`);
  if (row) return row;
  row = document.createElement('div');
  row.className = 'upm-item';
  row.dataset.id = item.id;
  row.dataset.st = item.status;
  const th = document.createElement('img');
  th.className = 'th';
  th.alt = '';
  if (item.previewUrl) th.src = item.previewUrl;
  const info = document.createElement('div');
  info.className = 'upm-info';
  const nm = document.createElement('div');
  nm.className = 'upm-name';
  nm.textContent = item.name;
  nm.title = item.name;
  const sub = document.createElement('div');
  sub.className = 'upm-sub';
  const bar = document.createElement('div');
  bar.className = 'upm-bar';
  const fill = document.createElement('div');
  bar.appendChild(fill);
  info.appendChild(nm); info.appendChild(sub); info.appendChild(bar);
  const st = document.createElement('div');
  st.className = 'upm-st';
  row.appendChild(th); row.appendChild(info); row.appendChild(st);
  upmList.appendChild(row);
  return row;
}
function upmPaintItem(item) {
  const row = upmRow(item);
  row.dataset.st = item.status;
  const fill = row.querySelector('.upm-bar > div');
  const st = row.querySelector('.upm-st');
  const sub = row.querySelector('.upm-sub');
  const pct = item.size > 0 ? Math.min(100, Math.round(item.loaded / item.size * 100)) : (item.status === 'done' ? 100 : 0);
  fill.style.width = `${item.status === 'queued' ? 0 : (item.status === 'processing' ? 100 : pct)}%`;
  if (item.status === 'queued') { st.textContent = '待機中'; sub.textContent = fmtBytes(item.size); }
  else if (item.status === 'uploading') { st.textContent = `${pct}%`; sub.textContent = `${fmtBytes(item.loaded)} / ${fmtBytes(item.size)}`; }
  else if (item.status === 'processing') { st.textContent = '処理中…'; sub.textContent = `${fmtBytes(item.size)} 送信済み`; }
  else if (item.status === 'done') { st.textContent = '完了'; sub.textContent = fmtBytes(item.size); }
  else if (item.status === 'skipped') { st.textContent = 'スキップ'; sub.textContent = '重複のためスキップ'; }
  else if (item.status === 'error') { st.textContent = 'エラー'; sub.textContent = item.error || '失敗'; }
  else if (item.status === 'cancelled') { st.textContent = '中止'; sub.textContent = fmtBytes(item.size); }
}
function upmPaintOverall() {
  const items = upm.items;
  const total = items.length;
  const doneN = items.filter(x => ['done', 'skipped', 'error', 'cancelled'].includes(x.status)).length;
  const okN = items.filter(x => x.status === 'done').length;
  const skipN = items.filter(x => x.status === 'skipped').length;
  const errN = items.filter(x => x.status === 'error').length;
  const cancelN = items.filter(x => x.status === 'cancelled').length;
  const sent = items.reduce((a, x) => a + Math.min(x.loaded, x.size), 0);
  const all = items.reduce((a, x) => a + x.size, 0) || 1;
  const pct = Math.min(100, Math.round(sent / all * 100));
  upmFill.style.width = `${pct}%`;
  upmTrack.className = 'upm-track' + (errN ? ' err' : (doneN === total && total ? ' done' : ''));
  upmCount.textContent = total ? `${doneN} / ${total}` : '';
  if (!total) {
    upmTitle.textContent = 'アップロード';
    upmSummary.textContent = '';
    return;
  }
  if (upm.running) {
    const now = Date.now();
    const dt = (now - upm.lastT) / 1000;
    if (dt >= 0.5 && dt > 0) {
      upm.speed = upm.speed * 0.6 + ((sent - upm.lastSent) / dt) * 0.4;
      upm.lastT = now; upm.lastSent = sent;
    }
    const activeN = items.filter(x => x.status === 'uploading' || x.status === 'processing').length;
    const cur = items.find(x => x.status === 'uploading' || x.status === 'processing');
    let extra = '';
    if (upm.speed > 0 && sent < all) extra = ` ・ ${fmtSpeed(upm.speed)} ・ ${fmtEta((all - sent) / upm.speed)}`;
    upmTitle.textContent = 'アップロード中';
    upmSummary.textContent = `${cur ? cur.name + ' を送信中' : '送信中…'}（${doneN} / ${total} 件）${extra}`;
    void activeN;
  } else {
    upmTitle.textContent = errN ? 'アップロード（エラーあり）' : 'アップロード完了';
    const parts = [`${okN} 件追加`];
    if (skipN) parts.push(`${skipN} 件スキップ`);
    if (errN) parts.push(`${errN} 件エラー`);
    if (cancelN) parts.push(`${cancelN} 件中止`);
    const secs = Math.max(1, Math.round((Date.now() - upm.startedAt) / 1000));
    upmSummary.textContent = `完了: ${parts.join('、')}（${secs}秒・合計${fmtBytes(all)}）`;
  }
  upmRetry.style.display = (!upm.running && errN) ? 'block' : 'none';
  document.getElementById('upm-cancel').style.display = upm.running ? 'block' : 'none';
}
function upmPump() {
  if (!upm.running || upm.cancelled) return;
  while (upm.active < UPM_PARALLEL) {
    const item = upm.items.find(x => x.status === 'queued');
    if (!item) break;
    upm.active++;
    item.status = 'uploading';
    upmPaintItem(item); upmPaintOverall();
    upmUploadOne(item).finally(() => {
      upm.active--;
      upmPaintItem(item); upmPaintOverall();
      if (upm.cancelled) { upmFinishIfIdle(); return; }
      upmPump();
      upmFinishIfIdle();
    });
  }
  upmPaintOverall();
}
function upmUploadOne(item) {
  return new Promise(resolve => {
    const fd = new FormData();
    fd.append('files', item.file, item.name);
    fd.append('manifest', JSON.stringify({ [item.name]: item.file.lastModified }));
    const xhr = new XMLHttpRequest();
    item.xhr = xhr;
    xhr.open('POST', '/api/upload', true);
    xhr.timeout = 0;
    xhr.upload.onprogress = e => {
      if (e.lengthComputable) item.loaded = e.loaded;
      else item.loaded = Math.max(item.loaded, 0);
      if (item.status === 'uploading') upmPaintItem(item);
      upmPaintOverall();
    };
    xhr.onload = () => {
      item.xhr = null;
      let j = null;
      try { j = JSON.parse(xhr.responseText); } catch (err) { /* noop */ }
      if (xhr.status >= 200 && xhr.status < 300 && j && (j.ok || (j.results && j.results.length))) {
        const r = (j.results || [])[0] || {};
        item.loaded = item.size;
        if (r.duplicate) item.status = 'skipped';
        else item.status = 'done';
        item.error = '';
      } else {
        const msg = (j && (j.error || ((j.errors || [])[0] || {}).error)) || `HTTP ${xhr.status}`;
        item.status = 'error';
        item.error = String(msg).slice(0, 120);
      }
      resolve();
    };
    xhr.onerror = () => {
      item.xhr = null;
      if (item.status !== 'cancelled') {
        item.status = upm.cancelled ? 'cancelled' : 'error';
        item.error = upm.cancelled ? '' : '通信エラー';
      }
      resolve();
    };
    xhr.onabort = () => {
      item.xhr = null;
      if (item.status !== 'done' && item.status !== 'skipped') item.status = 'cancelled';
      resolve();
    };
    xhr.ontimeout = () => {
      item.xhr = null;
      item.status = 'error';
      item.error = 'タイムアウト';
      resolve();
    };
    // 送信完了〜応答待ちはサーバ側の保存・サムネイル生成中
    xhr.upload.onload = () => {
      if (item.status === 'uploading') {
        item.loaded = item.size;
        item.status = 'processing';
        upmPaintItem(item); upmPaintOverall();
      }
    };
    try { xhr.send(fd); }
    catch (err) {
      item.status = 'error';
      item.error = String(err).slice(0, 120);
      resolve();
    }
  });
}
function upmFinishIfIdle() {
  const busy = upm.items.some(x => x.status === 'queued' || x.status === 'uploading' || x.status === 'processing');
  if (upm.active > 0 || busy) return;
  upm.running = false;
  upm.items.forEach(x => { x.xhr = null; });
  upmPaintOverall();
  upm.items.forEach(upmPaintItem);
  upmBox.classList.add('on');
  upmBox.classList.remove('min');
  loadMonths().catch(() => {});
  reload();
}
let upmIdSeq = 0;
async function uploadFiles(files) {
  if (!files || !files.length) return;
  const supported = [...files].filter(f =>
    /\.(jpe?g|png|heic|heif|webp|avif|tiff?|bmp|gif|mp4|mov|m4v|avi|mkv|webm|3gp|mts|m2ts|wmv)$/i.test(f.name));
  if (!supported.length) { alert('対応していないファイルです'); return; }
  const fresh = !upm.running;
  if (fresh) {
    upm.items.forEach(x => { if (x.previewUrl) URL.revokeObjectURL(x.previewUrl); });
    upm.items = [];
    upmList.innerHTML = '';
    upm.cancelled = false;
    upm.active = 0;
    upm.startedAt = Date.now();
    upm.lastT = Date.now();
    upm.lastSent = 0;
    upm.speed = 0;
  } else {
    upm.cancelled = false;
  }
  for (const f of supported) {
    const item = {
      id: ++upmIdSeq, file: f, name: f.name || 'upload',
      size: f.size || 0, loaded: 0, status: 'queued', error: '',
      xhr: null, previewUrl: null,
    };
    try {
      if (f.type && f.type.startsWith('image/')) item.previewUrl = URL.createObjectURL(f);
    } catch (err) { /* プレビュー無しで続行 */ }
    upm.items.push(item);
    upmPaintItem(item);
  }
  upm.running = true;
  upmBox.classList.add('on');
  upmBox.classList.remove('min');
  upmPaintOverall();
  upmPump();
  upmFinishIfIdle();
}
document.getElementById('upm-min').addEventListener('click', e => {
  e.stopPropagation();
  upmBox.classList.toggle('min');
});
document.getElementById('upm-head').addEventListener('click', () => {
  upmBox.classList.toggle('min');
});
document.getElementById('upm-cancel').addEventListener('click', e => {
  e.stopPropagation();
  if (!upm.running) return;
  if (!confirm('アップロードを中止しますか？（送信済みの分は保存されています）')) return;
  upm.cancelled = true;
  upm.items.forEach(x => {
    if (x.status === 'queued') x.status = 'cancelled';
    else if (x.xhr && (x.status === 'uploading' || x.status === 'processing')) {
      try { x.xhr.abort(); } catch (err) { /* noop */ }
    }
    upmPaintItem(x);
  });
  upmPaintOverall();
});
document.getElementById('upm-close').addEventListener('click', () => {
  if (upm.running) {
    upmBox.classList.remove('on');
    return;
  }
  upmBox.classList.remove('on');
});
upmRetry.addEventListener('click', () => {
  const failed = upm.items.filter(x => x.status === 'error');
  if (!failed.length) return;
  failed.forEach(x => { x.status = 'queued'; x.loaded = 0; x.error = ''; x.xhr = null; upmPaintItem(x); });
  upm.cancelled = false;
  upm.running = true;
  upm.startedAt = Date.now();
  upm.lastT = Date.now();
  upm.lastSent = upm.items.reduce((a, x) => a + Math.min(x.loaded, x.size), 0);
  upmBox.classList.remove('min');
  upmPaintOverall();
  upmPump();
});

loadMonths();
loadPhotos();
refreshSidebarBackup();
setInterval(refreshSidebarBackup, 15000);
</script>
</body>
</html>
"""


def main() -> None:
    common.PHOTO_DIR.mkdir(parents=True, exist_ok=True)
    common.THUMB_DIR.mkdir(parents=True, exist_ok=True)
    common.EDIT_PHOTO_DIR.mkdir(parents=True, exist_ok=True)
    common.init_db()
    from . import backup
    backup.ensure_watch()
    server = ThreadingHTTPServer((common.HOST, common.PORT), Handler)
    server.daemon_threads = True

    def stop(signum, frame):
        # shutdown() をメインスレッドで呼ぶと serve_forever() とデッドロックするため
        # 別スレッドで実行する
        print(f"signal {signum}, shutting down", file=sys.stderr)
        import threading
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    print(f"selfphoto server on http://{common.HOST}:{common.PORT}", file=sys.stderr)
    server.serve_forever()


if __name__ == "__main__":
    main()
