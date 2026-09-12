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
    server_version = "selfphoto/0.0.3"

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
                    dest = ingest.save_upload(filename, payload, fallback_ts=fallback_ts)
                    pid = ingest.finalize_upload(dest, fallback_ts=fallback_ts)
                    results.append({"name": filename, "path": dest.relative_to(C.PHOTO_DIR).as_posix(), "id": pid})
                except Exception as e:  # noqa: BLE001
                    errors.append({"name": filename, "error": str(e)})
                finally:
                    payload.close()
        except Exception as e:  # noqa: BLE001
            self.send_json({"error": f"upload failed: {e}", "results": results, "errors": errors}, 500)
            return
        self.send_json({"ok": True, "count": len(results), "results": results, "errors": errors})

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
        elif path == "/api/months":
            self.api_months()
        elif path == "/api/search":
            self.api_search(parsed.query)
        elif path.startswith("/thumb/"):
            self.serve_thumb(path[len("/thumb/"):])
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

        リポジトリを一時ディレクトリに clone し、install.sh を実行する
        （プログラム一式の上書き・systemd ユニット再登録。写真・DB は保持）。
        systemd 環境ではレスポンス後に selfphoto-server.service を再起動して
        新しいコードを読み込ませる。
        """
        import tempfile

        repo = os.environ.get(
            "SELFPHPHOTO_UPDATE_REPO", "https://github.com/hirogura/selfphoto.git")
        home = os.environ.get("SELFPHPHOTO_HOME", str(common.PROGRAM_DIR.parent))
        if not shutil.which("git"):
            self.send_json({"ok": False, "error": "git not found"}, 500)
            return
        try:
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
                installer = repo_dir / "install.sh"
                if not installer.is_file():
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

        q = parse_qs(query)
        limit = min(int(q.get("limit", ["500"])[0]), 2000)
        offset = max(int(q.get("offset", ["0"])[0]), 0)
        month = q.get("month", [None])[0]
        conn = common.get_db()
        sql = ("SELECT id, path, filename, captured_at, captured_local, is_video,"
               " width, height, camera, size, thumb_done FROM photos")
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
            thumb = f"/thumb/{Path(rel).with_suffix('').as_posix()}_thumb.webp" if r["thumb_done"] == 1 else None
            if base_url:
                thumb = base_url + thumb if thumb else None
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
                "original": f"/photo/{rel}",
            })
        self.send_json({"photos": photos, "count": len(photos)})

    def api_search(self, query: str) -> None:
        from urllib.parse import parse_qs

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
            " width, height, camera, size, thumb_done FROM photos"
            " WHERE filename LIKE ? OR camera LIKE ? OR path LIKE ?"
            " ORDER BY captured_at DESC, id DESC LIMIT ? OFFSET ?",
            (like, like, like, limit, offset),
        ).fetchall()
        photos = []
        for r in rows:
            rel = r["path"]
            thumb = f"/thumb/{Path(rel).with_suffix('').as_posix()}_thumb.webp" if r["thumb_done"] == 1 else None
            photos.append({
                "id": r["id"], "path": rel, "filename": r["filename"],
                "capturedAt": r["captured_at"], "capturedLocal": r["captured_local"],
                "isVideo": bool(r["is_video"]), "width": r["width"], "height": r["height"],
                "camera": r["camera"], "size": r["size"], "thumb": thumb,
                "original": f"/photo/{rel}",
            })
        self.send_json({"photos": photos, "count": len(photos)})

    def api_months(self) -> None:
        conn = common.get_db()
        rows = conn.execute(
            "SELECT year, month, COUNT(*) AS n FROM photos GROUP BY month ORDER BY month DESC"
        ).fetchall()
        months = [{"year": r["year"], "month": r["month"], "count": r["n"]} for r in rows]
        total = conn.execute("SELECT COUNT(*) AS n FROM photos").fetchone()["n"]
        self.send_json({"months": months, "total": total})

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
/* ---------------- sidebar ---------------- */
#sidebar {
  position: fixed; top: 0; bottom: 0; left: 0; width: 220px; z-index: 30;
  background: var(--sidebar); border-right: 1px solid var(--line);
  display: flex; flex-direction: column; padding: 14px 10px;
}
#sidebar .logo {
  display: flex; align-items: flex-end; gap: 7px;
  font-weight: 700; font-size: 17px; padding: 6px 10px 14px; letter-spacing: .3px;
}
#sidebar .logo .logo-icon {
  width: 20px; height: 20px; border-radius: 5px; object-fit: cover;
  margin-bottom: 2px; flex: none;
}
#sidebar .logo .ver {
  color: var(--muted); font-weight: 400; font-size: 11px; letter-spacing: 0;
  margin: 0 0 2px -1px;
}
#sidebar .logo .ver {
  color: var(--muted); font-weight: 400; font-size: 11px; letter-spacing: 0;
  margin-left: -2px; align-self: flex-start; margin-top: 1px;
}
#sidebar .foot #restart-btn, #sidebar .foot #update-btn {
  width: 100%; display: flex; align-items: center; justify-content: center; gap: 6px;
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

/* ---------------- top bar ---------------- */
header {
  position: sticky; top: 0; z-index: 10;
  display: flex; align-items: center; gap: 12px;
  padding: 10px 16px; background: rgba(14,15,17,.92); backdrop-filter: blur(6px);
  border-bottom: 1px solid #222;
}
header h1 { font-size: 16px; margin: 0; font-weight: 650; letter-spacing: .3px; }
header .count { color: var(--muted); font-size: 12px; }
header .spacer { flex: 1; }
#search-box {
  flex: 1; max-width: 340px; display: none;
  background: var(--chip); border: 1px solid #33363c; border-radius: 10px;
  padding: 7px 12px; color: var(--fg); font-size: 14px; outline: none;
}
#search-box:focus { border-color: var(--accent); }
main { padding: 0 8px 80px 228px; }

/* ---------------- timeline ---------------- */
.month-head {
  position: sticky; top: 52px; z-index: 9;
  padding: 14px 10px 6px; font-weight: 700; font-size: 15px;
  background: linear-gradient(var(--bg), rgba(14,15,17,.85));
}
.grid {
  display: grid; gap: 3px;
  grid-template-columns: repeat(auto-fill, minmax(140px, 1fr));
}
@media (max-width: 640px) { .grid { grid-template-columns: repeat(3, 1fr); } }
.cell {
  position: relative; aspect-ratio: 1/1; overflow: hidden;
  background: var(--card); border-radius: 4px; cursor: pointer;
}
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
#lightbox img, #lightbox video {
  max-width: 100%; max-height: 100%; object-fit: contain;
}
#lightbox .bar {
  position: fixed; top: 0; left: 0; right: 0;
  display: flex; justify-content: space-between; align-items: center;
  padding: 10px 14px; color: #ddd; font-size: 13px;
  background: linear-gradient(rgba(0,0,0,.6), transparent);
}
#lightbox button {
  background: var(--chip); color: var(--fg); border: 0; border-radius: 8px;
  padding: 8px 12px; font-size: 14px; cursor: pointer;
}
#lightbox .nav {
  position: fixed; top: 50%; transform: translateY(-50%);
  font-size: 26px; padding: 14px 16px; opacity: .75;
}
#prev { left: 8px; } #next { right: 8px; }
#loading { text-align: center; color: var(--muted); padding: 24px; }
/* ---------------- header action buttons ---------------- */
.header-actions { display: flex; gap: 6px; margin-left: auto; flex: none; }
.header-actions button {
  background: var(--chip); color: var(--fg); border: 0; border-radius: 8px;
  padding: 6px 12px; font-size: 13px; cursor: pointer; flex: none;
}
.header-actions button:hover { background: #33363c; }
.header-actions button.on { background: var(--accent); color: #fff; }
#download-btn { display: none; }
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
@media (max-width: 760px) {
  #sidebar { width: 60px; padding: 14px 6px; }
  #sidebar .logo span.txt, #sidebar nav button span.lbl, #sidebar .foot { display: none; }
  #sidebar nav button { justify-content: center; padding: 12px 0; }
  main { padding-left: 66px; }
  #up-bar { left: 60px; }
  #search-box { max-width: none; }
  #scrubber { display: none; }
}
</style>
</head>
<body>
<aside id="sidebar">
  <div class="logo"><img class="logo-icon" src="/icon/selfphotofav.png" alt=""><span class="txt">selfphoto</span><span class="ver">v.{__VERSION__}</span></div>
  <nav>
    <button id="nav-photos" class="active"><span class="ico"><svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="4" width="18" height="16" rx="2.5"/><circle cx="8.5" cy="9.5" r="1.7"/><path d="M21 16l-5-5-9 9"/></svg></span><span class="lbl">写真</span></button>
    <button id="nav-search"><span class="ico"><svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><circle cx="11" cy="11" r="6.5"/><path d="M20 20l-4.2-4.2"/></svg></span><span class="lbl">検索</span></button>
    <button id="nav-upload"><span class="ico"><svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M12 16V4"/><path d="M6.5 9.5L12 4l5.5 5.5"/><path d="M4 20h16"/></svg></span><span class="lbl">アップロード</span></button>
  </nav>
  <div class="foot">
    <button id="restart-btn"><svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M20 12a8 8 0 1 1-2.34-5.66"/><path d="M20 3v4h-4"/></svg><span>再起動</span></button>
    <button id="update-btn"><svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M12 4v11"/><path d="M6.5 10.5L12 16l5.5-5.5"/><path d="M4 20h16"/></svg><span>アップデート</span></button>
  </div>
</aside>
<div id="restart-ov"><div class="ro-inner" id="ro-text">再起動中…</div></div>
<header>
  <h1 id="view-title">写真</h1>
  <span class="count" id="count"></span>
  <input id="search-box" type="search" placeholder="ファイル名・カメラで検索…" autocomplete="off">
  <div class="spacer"></div>
  <div class="header-actions">
    <button id="download-btn"><svg viewBox="0 0 24 24" width="15" height="15" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" style="vertical-align:-2px"><path d="M12 4v11"/><path d="M6.5 10.5L12 16l5.5-5.5"/><path d="M4 20h16"/></svg> ダウンロード</button>
    <button id="select-btn"><svg viewBox="0 0 24 24" width="15" height="15" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" style="vertical-align:-2px"><rect x="4" y="4" width="16" height="16" rx="3"/><path d="M8.5 12.5l2.5 2.5 5-5.5"/></svg> 選択</button>
    <button id="add-btn"><svg viewBox="0 0 24 24" width="15" height="15" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" style="vertical-align:-2px"><path d="M12 16V4"/><path d="M6.5 9.5L12 4l5.5 5.5"/><path d="M4 20h16"/></svg> アップロード</button>
  </div>
</header>
<main>
  <div id="timeline"></div>
  <div id="loading">読み込み中…</div>
</main>
<div id="scrubber"></div>
<div id="now-viewing"></div>
<input type="file" id="file-input" multiple accept="image/*,video/*" style="display:none">
<div id="dropzone"><div class="dz-inner">ドロップでアップロード</div></div>
<div id="up-bar"><div id="up-label"></div><div id="up-track"><div id="up-fill"></div></div></div>
<div id="lightbox">
  <div class="bar"><span id="lb-title"></span><button id="lb-close">閉じる ✕</button></div>
  <button class="nav" id="prev">‹</button>
  <button class="nav" id="next">›</button>
  <div id="lb-content"></div>
</div>
<script>
const state = { view: 'photos', month: null, term: '', offset: 0, limit: 500, done: false, photos: [], selecting: false, selected: new Set() };
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
const viewTitle = document.getElementById('view-title');
let searchTimer = null;

document.getElementById('nav-photos').onclick = () => setView('photos');
document.getElementById('nav-search').onclick = () => { setView('search'); searchBox.focus(); };
document.getElementById('nav-upload').onclick = () => fileInput.click();

function setView(v) {
  state.view = v;
  document.querySelectorAll('#sidebar nav button').forEach(b => b.classList.remove('active'));
  document.getElementById('nav-' + v).classList.add('active');
  viewTitle.textContent = v === 'search' ? '検索' : '写真';
  searchBox.style.display = v === 'search' ? 'block' : 'none';
  if (v === 'search') {
    if (!state.term) state.term = '';
    reload();
  } else {
    state.term = '';
    searchBox.value = '';
    reload();
  }
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
  document.getElementById('count').textContent = `${j.total} 枚`;
}

async function loadPhotos() {
  if (state.done) return;
  document.getElementById('loading').textContent = '読み込み中…';
  const q = new URLSearchParams({ limit: state.limit, offset: state.offset });
  if (state.view === 'search') {
    q.set('q', state.term);
  }
  const url = state.view === 'search' ? '/api/search' : '/api/photos';
  const r = await fetch(url + '?' + q);
  const j = await r.json();
  if (j.photos.length < state.limit) state.done = true;
  state.offset += j.photos.length;
  state.photos.push(...j.photos);
  render();
  document.getElementById('loading').textContent = state.done ? '' : 'もっと読み込む…';
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
      const all = inMonth.every(p => state.selected.has(p.path));
      inMonth.forEach(p => all ? state.selected.delete(p.path) : state.selected.add(p.path));
      refreshSelectionUi();
    });
  } else {
    box.addEventListener('click', e => {
      e.stopPropagation();
      const all = paths.every(p => state.selected.has(p));
      paths.forEach(p => all ? state.selected.delete(p) : state.selected.add(p));
      refreshSelectionUi();
    });
  }
  return box;
}

function refreshSelectionUi() {
  document.body.classList.toggle('selecting', state.selecting);
  document.getElementById('select-btn').textContent = state.selecting ? '解除' : '選択';
  document.getElementById('select-btn').classList.toggle('on', state.selecting);
  document.getElementById('download-btn').style.display = state.selecting ? 'block' : 'none';
  // セルの表示更新
  document.querySelectorAll('.cell').forEach(c => {
    c.classList.toggle('selected', state.selected.has(c.dataset.path));
  });
  // 見出しのチェック表示更新
  document.querySelectorAll('.day-head').forEach(dh => {
    const folder = dh.dataset.folder;
    if (!folder) return;
    const inFolder = state.photos.filter(p => p.path.startsWith(folder + '/')).map(p => p.path);
    const n = inFolder.filter(p => state.selected.has(p)).length;
    const box = dh.querySelector('.sel-box');
    if (box) {
      box.textContent = n === 0 ? '' : (n === inFolder.length && inFolder.length > 0 ? '✓' : String(n));
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
      box.textContent = n === 0 ? '' : (n === inMonth.length && inMonth.length > 0 ? '✓' : String(n));
      mh.classList.toggle('on', n > 0);
    }
  });
}

function makeCell(p) {
  const c = document.createElement('div');
  c.className = 'cell';
  c.dataset.path = p.path;
  const img = document.createElement('img');
  img.loading = 'lazy';
  img.dataset.src = p.thumb || p.original;
  img.alt = p.filename;
  img.addEventListener('click', () => {
    if (state.selecting) { toggleSel(p); return; }
    openLb(p);
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
  refreshSelectionUi();
}

// ---------------- header actions ----------------
document.getElementById('select-btn').addEventListener('click', () => {
  state.selecting = !state.selecting;
  if (!state.selecting) state.selected.clear();
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

document.getElementById('download-btn').addEventListener('click', async () => {
  const sel = state.photos.filter(p => state.selected.has(p.path));
  if (!sel.length) { alert('ダウンロードする写真を選択してください'); return; }
  // 日付フォルダごとにグループ化（フォルダ丸ごと選択されていたら zip、それ以外は 1 枚ずつ）
  const byFolder = new Map();
  for (const p of sel) {
    const seg = p.path.split('/');
    const folder = seg.length >= 3 ? seg.slice(0, 3).join('/') : '';
    if (!byFolder.has(folder)) byFolder.set(folder, []);
    byFolder.get(folder).push(p);
  }
  for (const [folder, items] of byFolder) {
    // フォルダ内の全ファイルが選択されている場合は zip 1 つで
    const allInFolder = state.photos.filter(p => p.path.startsWith(folder + '/'));
    const fullFolder = allInFolder.length > 0 && items.length === allInFolder.length;
    if (fullFolder && folder) {
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
  state.photos = []; state.offset = 0; state.done = false;
  render(); loadPhotos();
}

// ---------------- lightbox ----------------
let lbIndex = -1;
const lb = document.getElementById('lightbox');
const lbContent = document.getElementById('lb-content');
function openLb(p) {
  lbIndex = state.photos.findIndex(x => x.path === p.path);
  showLb();
}
function showLb() {
  const p = state.photos[lbIndex];
  if (!p) return;
  lb.classList.add('open');
  lbContent.innerHTML = '';
  let el;
  if (p.isVideo) {
    el = document.createElement('video');
    el.controls = true; el.autoplay = true;
    el.src = p.original;
  } else {
    el = document.createElement('img');
    el.src = p.original;
  }
  lbContent.appendChild(el);
  document.getElementById('lb-title').textContent =
    `${p.filename}　${p.camera || ''} ${p.width||''}×${p.height||''}`;
  history.replaceState(null, '', '#p=' + encodeURIComponent(p.path));
}
function moveLb(delta) {
  if (lbIndex < 0) return;
  lbIndex = (lbIndex + delta + state.photos.length) % state.photos.length;
  showLb();
}
document.getElementById('lb-close').onclick = () => { lb.classList.remove('open'); lbContent.innerHTML = ''; };
document.getElementById('prev').onclick = () => moveLb(-1);
document.getElementById('next').onclick = () => moveLb(1);
document.addEventListener('keydown', e => {
  if (!lb.classList.contains('open')) return;
  if (e.key === 'Escape') document.getElementById('lb-close').click();
  if (e.key === 'ArrowLeft') moveLb(-1);
  if (e.key === 'ArrowRight') moveLb(1);
});
lb.addEventListener('click', e => { if (e.target === lb) document.getElementById('lb-close').click(); });

// infinite scroll は上の scroll ハンドラに統合

// ---------------- upload (file picker + drag & drop) ----------------
const fileInput = document.getElementById('file-input');
const dropzone = document.getElementById('dropzone');
const upBar = document.getElementById('up-bar');
const upLabel = document.getElementById('up-label');
const upFill = document.getElementById('up-fill');
document.getElementById('add-btn').addEventListener('click', () => fileInput.click());
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

const CHUNK = 5;
let uploading = false;

async function uploadFiles(files) {
  if (!files || !files.length || uploading) return;
  const supported = [...files].filter(f =>
    /\.(jpe?g|png|heic|heif|webp|avif|tiff?|bmp|gif|mp4|mov|m4v|avi|mkv|webm|3gp|mts|m2ts|wmv)$/i.test(f.name));
  if (!supported.length) { alert('対応していないファイルです'); return; }
  uploading = true;
  upBar.classList.add('on');
  const total = supported.length;
  let done = 0, okCount = 0, errCount = 0;
  for (let i = 0; i < total; i += CHUNK) {
    const slice = supported.slice(i, i + CHUNK);
    const fd = new FormData();
    const manifest = {};
    for (const f of slice) { fd.append('files', f, f.name); manifest[f.name] = f.lastModified; }
    fd.append('manifest', JSON.stringify(manifest));
    upLabel.textContent = `アップロード中… ${done + 1}−${Math.min(done + slice.length, total)} / ${total}`;
    upFill.style.width = `${(done / total) * 100}%`;
    try {
      const r = await fetch('/api/upload', { method: 'POST', body: fd });
      const j = await r.json();
      okCount += j.count || 0;
      errCount += (j.errors || []).length;
    } catch (err) {
      errCount += slice.length;
    }
    done += slice.length;
    upFill.style.width = `${(done / total) * 100}%`;
    upLabel.textContent = `アップロード中… ${done} / ${total}`;
  }
  upLabel.textContent = `完了: ${okCount} 件追加${errCount ? `, ${errCount} 件エラー` : ''}`;
  setTimeout(() => { upBar.classList.remove('on'); upFill.style.width = '0'; }, 3500);
  uploading = false;
  await Promise.all([loadMonths(), reload()]);
}

loadMonths();
loadPhotos();
</script>
</body>
</html>
"""


def main() -> None:
    common.init_db()
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
