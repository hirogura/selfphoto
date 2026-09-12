"""撮影日時・サイズの抽出。

 Pillow があれば使う。なければ標準ライブラリのみで動く
 （JPEG/TIFF は純正 Python パーサ、PNG は標準モジュール、
  HEIC/VIDEO はファイル日時にフォールバック）。
"""
from __future__ import annotations

import os
import struct
from datetime import datetime, timezone
from pathlib import Path

try:
    from PIL import Image, ExifTags  # type: ignore
    HAS_PIL = True
except Exception:  # pragma: no cover
    Image = None
    ExifTags = None
    HAS_PIL = False

# ---------------------------------------------------------------------------
# Pillow を使う場合
# ---------------------------------------------------------------------------

def _pil_capture(path: Path) -> datetime | None:
    try:
        with Image.open(path) as im:
            exif = im.getexif()
        if not exif:
            return None
        tag_for_name = {}
        if ExifTags is not None:
            tag_for_name = {v: k for k, v in ExifTags.TAGS.items()}
        dt = None
        # DateTimeOriginal (撮影日時) を最優先
        tag = tag_for_name.get("DateTimeOriginal")
        if tag is not None and tag in exif:
            dt = str(exif[tag])
        elif tag is not None:
            # IFD 内を探す
            try:
                exif_ifd = exif.get_ifd(0x8769)
                if tag in exif_ifd:
                    dt = str(exif_ifd[tag])
            except Exception:
                pass
        if dt is None:
            tag = tag_for_name.get("DateTime")
            if tag is not None and tag in exif:
                dt = str(exif[tag])
        if not dt:
            return None
        return datetime.strptime(dt.strip(), "%Y:%m:%d %H:%M:%S")
    except Exception:
        return None


def _pil_size(path: Path) -> tuple[int, int] | None:
    try:
        with Image.open(path) as im:
            w, h = im.size
            # Exif Orientation が縦向き (5〜8) なら表示サイズは入れ替わる
            try:
                exif = im.getexif()
                if exif and exif.get(0x0112) in (5, 6, 7, 8):
                    w, h = h, w
            except Exception:
                pass
        return int(w), int(h)
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Pillow 純正 Python フォールバック（JPEG/TIFF の Exif のみ）
# ---------------------------------------------------------------------------

_TIFF_TYPES = {1: ("B", 1), 2: ("s", 1), 3: ("H", 2), 4: ("I", 4), 5: ("II", 8),
               6: ("b", 1), 7: ("s", 1), 8: ("h", 2), 9: ("i", 4), 10: ("ii", 8),
               11: ("f", 4), 12: ("d", 8)}

EXIF_DATETIME_ORIGINAL = 0x9003
EXIF_DATETIME = 0x0132
EXIF_OFFSET_TAG = 0x8769


def _pure_capture(path: Path) -> datetime | None:
    try:
        data = path.read_bytes()[:256 * 1024]
    except Exception:
        return None
    if data[:2] == b"\xff\xd8":  # JPEG
        return _pure_jpeg_capture(data)
    if data[:2] in (b"II", b"MM"):  # TIFF
        return _pure_tiff_capture(data)
    return None


def _pure_jpeg_capture(data: bytes) -> datetime | None:
    i = 2
    n = len(data)
    while i + 4 <= n:
        if data[i] != 0xFF:
            i += 1
            continue
        marker = data[i + 1]
        if marker in (0xD8, 0xD9) or 0xD0 <= marker <= 0xD7:
            i += 2
            continue
        if i + 4 > n:
            break
        seglen = struct.unpack(">H", data[i + 2:i + 4])[0]
        if marker == 0xE1 and data[i + 4:i + 10] == b"Exif\x00\x00":
            tiff = data[i + 10:i + seglen - 2]
            dt = _parse_tiff_dt(tiff, (EXIF_DATETIME_ORIGINAL, EXIF_DATETIME))
            if dt:
                return dt
        i += 2 + seglen
    return None


def _parse_tiff_dt(tiff: bytes, wanted: tuple[int, ...]) -> datetime | None:
    if len(tiff) < 8:
        return None
    endian = "<" if tiff[:2] == b"II" else ">"
    off = struct.unpack(endian + "I", tiff[4:8])[0]
    result = None
    for _pass in range(2):  # 1 周目: IFD0 (DateTime)、2 周目: Exif IFD (DateTimeOriginal)
        if off + 2 > len(tiff):
            break
        count = struct.unpack(endian + "H", tiff[off:off + 2])[0]
        next_off = 0
        exif_ifd_off = None
        for k in range(count):
            e = off + 2 + k * 12
            if e + 12 > len(tiff):
                break
            tag, typ, cnt = struct.unpack(endian + "HHI", tiff[e:e + 8])
            valoff = e + 8
            if typ not in _TIFF_TYPES:
                continue
            fmt, unit = _TIFF_TYPES[typ]
            size = unit * cnt
            if size > 4:
                p = struct.unpack(endian + "I", tiff[valoff:valoff + 4])[0]
                valoff = p
            raw = tiff[valoff:valoff + size]
            if tag == EXIF_OFFSET_TAG:
                exif_ifd_off = struct.unpack(endian + "I", raw[:4])[0] if cnt >= 1 else None
            if tag in wanted:
                if typ == 2:  # ASCII
                    text = raw.rstrip(b"\x00").decode("ascii", "replace")
                    try:
                        result = datetime.strptime(text.strip(), "%Y:%m:%d %H:%M:%S")
                    except ValueError:
                        result = None
                    if result:
                        return result
        if result:
            return result
        off = exif_ifd_off if exif_ifd_off else 0
        if off == 0:
            break
    return None


def _pure_tiff_capture(data: bytes) -> datetime | None:
    return _parse_tiff_dt(data, (EXIF_DATETIME_ORIGINAL, EXIF_DATETIME))


# ---------------------------------------------------------------------------
# PNG のサイズ（標準モジュール）
# ---------------------------------------------------------------------------

def _png_size(path: Path) -> tuple[int, int] | None:
    try:
        with path.open("rb") as f:
            head = f.read(33)
        if len(head) >= 24 and head[:8] == b"\x89PNG\r\n\x1a\n" and head[12:16] == b"IHDR":
            w, h = struct.unpack(">II", head[16:24])
            return int(w), int(h)
    except Exception:
        pass
    return None


# ---------------------------------------------------------------------------
# 公開 API
# ---------------------------------------------------------------------------

def extract_capture_datetime(path: Path) -> datetime | None:
    """Exif から撮影日時 (UTC) を抽出。なければ None。"""
    dt_local = None
    if HAS_PIL:
        dt_local = _pil_capture(path)
    if dt_local is None and path.suffix.lower() in (".jpg", ".jpeg", ".tif", ".tiff"):
        dt_local = _pure_capture(path)
    if dt_local is None:
        return None
    # Exif は通常現地時刻。タイムゾーン情報がなければ UTC とみなして扱う
    # （自前運用なら並び順に影響はほぼない）
    if dt_local.tzinfo is None:
        dt_local = dt_local.replace(tzinfo=timezone.utc)
    return dt_local.astimezone(timezone.utc)


def capture_datetime(path: Path) -> datetime:
    """撮影日時を UTC datetime で返す。Exif がなければ mtime。"""
    dt = extract_capture_datetime(path)
    if dt is not None:
        return dt
    st = path.stat()
    return datetime.fromtimestamp(st.st_mtime, tz=timezone.utc)


def capture_datetime_local(path: Path) -> datetime:
    """ローカルタイムとしての撮影日時（フォルダ分割用）。"""
    dt = capture_datetime(path)
    if HAS_PIL:
        local = _pil_capture(path)
        if local is not None:
            return local
    # mtime の場合はローカル時刻に変換
    return dt.astimezone()


def image_size(path: Path) -> tuple[int, int] | None:
    suffix = path.suffix.lower()
    if HAS_PIL:
        size = _pil_size(path)
        if size:
            return size
    if suffix == ".png":
        return _png_size(path)
    return None


def camera_model(path: Path) -> str | None:
    if not HAS_PIL:
        return None
    try:
        with Image.open(path) as im:
            exif = im.getexif()
        if not exif:
            return None
        model = str(exif.get(0x0110, "")).strip() or None
        make = str(exif.get(0x010F, "")).strip() or None
        if model and make and not model.startswith(make):
            return f"{make} {model}"
        return model or make
    except Exception:
        return None
