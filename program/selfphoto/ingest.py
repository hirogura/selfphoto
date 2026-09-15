"""写真の取り込み: スキャン → DB 登録 → サムネイル生成。

CLI:
  python3 -m selfphoto.ingest scan           # データフォルダを走査して DB 登録
  python3 -m selfphoto.ingest thumbs [N]     # 未生成サムネイルを N 枚処理（省略時は全部）
  python3 -m selfphoto.ingest views [N]      # 未生成ビューア用プレビューを N 枚処理（省略時は全部）
  python3 -m selfphoto.ingest import SRC_DIR # SD カード等から日付フォルダへコピーして登録
"""
from __future__ import annotations

import hashlib
import os
import shutil
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

from . import common, exif


def file_hash_of(path: Path) -> str:
    h = hashlib.blake2b(digest_size=16)
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def capture_dates(path: Path, fallback_ts: float | None = None) -> tuple[datetime, datetime]:
    """(UTC の撮影日時, ローカルの撮影日時) を返す。

    Exif がなければ fallback_ts（なければ mtime）。
    Exif は現地の壁時計なので、フォルダ分割用のローカル日時は
    そのまま壁時計を使う（UTC 経由で astimezone すると JST で日付がずれる）。
    """
    dt_utc = exif.extract_capture_datetime(path)
    if dt_utc is None:
        ts = fallback_ts if fallback_ts is not None else path.stat().st_mtime
        dt_utc = datetime.fromtimestamp(ts, tz=timezone.utc)
        return dt_utc, dt_utc.astimezone()
    if exif.HAS_PIL:
        local = exif._pil_capture(path)
        if local is not None:
            return dt_utc, local
    # Pillow 無しで純正パーサが当てた場合: dt_utc は壁時計に UTC ラベルを
    # 付けたものなので、tz を外せば壁時計に戻る（日付ずれ防止）。
    return dt_utc, dt_utc.replace(tzinfo=None)


def register_file(path: Path, conn=None, file_hash: str | None = None,
                  fallback_ts: float | None = None) -> bool:
    """1 ファイルを DB に登録する。新規なら True。"""
    conn = conn or common.get_db()
    rel = path.relative_to(common.PHOTO_DIR).as_posix()
    if conn.execute("SELECT 1 FROM photos WHERE path=?", (rel,)).fetchone():
        return False
    st = path.stat()
    dt_utc, dt_local = capture_dates(path, fallback_ts=fallback_ts)
    suffix = path.suffix.lower()
    is_video = int(suffix in common.VIDEO_EXTS)
    size = exif.image_size(path) if not is_video else None
    row = (
        rel,
        path.name,
        dt_utc.isoformat(),
        dt_local.isoformat(),
        f"{dt_local.year:04d}",
        f"{dt_local.year:04d}{dt_local.month:02d}",
        is_video,
        size[0] if size else None,
        size[1] if size else None,
        exif.camera_model(path),
        st.st_size,
        st.st_mtime,
        file_hash or file_hash_of(path),
    )
    conn.execute(
        """INSERT OR IGNORE INTO photos
           (path, filename, captured_at, captured_local, year, month, is_video,
            width, height, camera, size, mtime, hash)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        row,
    )
    return True


def scan_data_dir(max_workers: int = 8) -> dict:
    """PHOTO_DIR (photo/) 以下を走査して DB に登録する。"""
    common.init_db()
    conn = common.get_db()
    known = {r["path"] for r in conn.execute("SELECT path FROM photos")}
    common.PHOTO_DIR.mkdir(parents=True, exist_ok=True)
    files = [
        p for p in common.PHOTO_DIR.rglob("*")
        if p.is_file() and p.suffix.lower() in common.SUPPORTED_EXTS
    ]

    def hash_one(p: Path):
        try:
            return p, file_hash_of(p)
        except OSError:
            return p, None

    added = 0
    # 重いハッシュ計算だけ並列化し、DB 登録はメインスレッドで行う
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        for p, h in pool.map(hash_one, files):
            if h is not None and register_file(p, conn, file_hash=h):
                added += 1
    # 消えたファイルを DB からも削除
    on_disk = {p.relative_to(common.PHOTO_DIR).as_posix() for p in files}
    gone = known - on_disk
    for rel in gone:
        conn.execute("DELETE FROM photos WHERE path=?", (rel,))
    conn.commit()
    print(f"scan: {len(files)} files, {added} new, {len(gone)} removed")
    return {"total": len(files), "added": added, "removed": len(gone)}


def thumb_rel_path(rel: str, base: str) -> str:
    return f"{Path(rel).with_suffix('').as_posix()}_{base}{common.THUMB_EXT}"


def view_rel_path(rel: str) -> str:
    """ビューア用プレビュー画像の相対パス（VIEW_DIR からの相対）。"""
    return f"{Path(rel).with_suffix('').as_posix()}_view{common.VIEW_EXT}"


def make_view_image(src: Path, dst: Path) -> bool:
    """1 枚のビューア用プレビュー画像を生成する。成功したら True。

    長辺 common.VIEW_SIZE px に縮小した WebP。Exif Orientation を反映する。
    """
    try:
        from PIL import Image, ImageOps
    except ImportError:
        return False
    try:
        dst.parent.mkdir(parents=True, exist_ok=True)
        with Image.open(src) as im:
            im.draft("RGB", (common.VIEW_SIZE * 2, common.VIEW_SIZE * 2))
            # WebP アニメ対策で先頭フレームのみ
            try:
                im.seek(0)
            except Exception:
                pass
            # Exif Orientation を反映（縦写真は縦向きのプレビューになる）
            try:
                im = ImageOps.exif_transpose(im)
            except Exception:
                pass
            if im is None:
                raise ValueError("exif_transpose failed")
            im = im.convert("RGB")
            im.thumbnail((common.VIEW_SIZE, common.VIEW_SIZE), Image.LANCZOS)
            im.save(dst, "WEBP", quality=common.VIEW_QUALITY, method=4)
        return True
    except Exception as e:
        print(f"view error {src}: {e}", file=sys.stderr)
        return False


def make_view(row) -> bool:
    """DB 行からビューア用プレビュー画像を生成する。成功したら True。"""
    if int(row["is_video"] or 0):
        return False
    src = common.PHOTO_DIR / row["path"]
    dst = common.VIEW_DIR / view_rel_path(row["path"])
    return make_view_image(src, dst)


def make_thumbnail(row) -> bool:
    """1 枚のサムネイルを生成する。成功したら True。"""
    try:
        from PIL import Image, ImageOps
    except ImportError:
        # Pillow 無しでは生成できない。thumb_done=0 のままにして後で再試行させる。
        return False

    src = common.PHOTO_DIR / row["path"]
    rel = thumb_rel_path(row["path"], "thumb")
    dst = common.THUMB_DIR / rel
    dst.parent.mkdir(parents=True, exist_ok=True)
    try:
        with Image.open(src) as im:
            im.draft("RGB", (common.THUMB_SIZE * 2, common.THUMB_SIZE * 2))
            # WebP アニメ対策で先頭フレームのみ
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
                raise ValueError("exif_transpose failed")
            im = im.convert("RGB")
            im.thumbnail((common.THUMB_SIZE, common.THUMB_SIZE), Image.LANCZOS)
            im.save(dst, "WEBP", quality=82, method=4)
        conn = common.get_db()
        conn.execute("UPDATE photos SET thumb_path=?, thumb_done=1 WHERE id=?",
                     (rel, row["id"]))
        conn.commit()
        return True
    except Exception as e:
        print(f"thumb error {row['path']}: {e}", file=sys.stderr)
        conn = common.get_db()
        conn.execute("UPDATE photos SET thumb_done=-1 WHERE id=?", (row["id"],))
        conn.commit()
        return False


def process_thumbnails(limit: int | None = None, max_workers: int = 4) -> None:
    common.init_db()
    conn = common.get_db()
    sql = "SELECT * FROM photos WHERE thumb_done=0 ORDER BY captured_at DESC"
    if limit:
        sql += f" LIMIT {int(limit)}"
    rows = conn.execute(sql).fetchall()
    ok = 0
    with ThreadPoolExecutor(max_workers=max_workers) as ex_:
        for r in ex_.map(make_thumbnail, rows):
            ok += 1 if r else 0
    print(f"thumbs: {ok}/{len(rows)} done")


def process_views(limit: int | None = None, max_workers: int = 4) -> None:
    """未生成のビューア用プレビュー画像をまとめて生成する（backfill 用）。

    DB に追跡列を持たないため、対応ファイルが無い・または元画像より古い
    ものを対象にする。動画は対象外。
    """
    common.init_db()
    conn = common.get_db()
    sql = "SELECT * FROM photos WHERE is_video=0 ORDER BY captured_at DESC"
    if limit:
        sql += f" LIMIT {int(limit)}"
    rows = conn.execute(sql).fetchall()
    targets = []
    for r in rows:
        try:
            src = common.PHOTO_DIR / r["path"]
            dst = common.VIEW_DIR / view_rel_path(r["path"])
            if not src.is_file():
                continue
            if dst.is_file() and dst.stat().st_mtime >= src.stat().st_mtime:
                continue
            targets.append(r)
        except OSError:
            continue
    ok = 0
    with ThreadPoolExecutor(max_workers=max_workers) as ex_:
        for r in ex_.map(make_view, targets):
            ok += 1 if r else 0
    print(f"views: {ok}/{len(targets)} done")


# ---------------------------------------------------------------------------
# import: ソースから日付フォルダへコピー
# ---------------------------------------------------------------------------

def _head_equal(a: Path, b: Path, size: int = 65536) -> bool:
    """先頭 size バイトが等しいか比較する（大容量ファイルの全読み込みを避ける）。"""
    try:
        with a.open("rb") as fa, b.open("rb") as fb:
            return fa.read(size) == fb.read(size)
    except OSError:
        return False


def import_source(src_dir: str, dry_run: bool = False) -> dict:
    src = Path(src_dir).resolve()
    if not src.is_dir():
        raise ValueError(f"not a directory: {src}")
    common.init_db()
    conn = common.get_db()
    files = [p for p in src.rglob("*")
             if p.is_file() and p.suffix.lower() in common.SUPPORTED_EXTS]
    print(f"import: {len(files)} files found under {src}")
    copied = skipped = 0
    for p in files:
        st = p.stat()
        dt_utc, dt_local = capture_dates(p)
        ymd = f"{dt_local.year:04d}{dt_local.month:02d}{dt_local.day:02d}"
        dest_dir = (common.PHOTO_DIR / f"{dt_local.year:04d}"
                    / f"{dt_local.year:04d}{dt_local.month:02d}" / f"{ymd}_")
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / p.name
        # 同名ファイルがある場合は _1, _2... を付ける
        n = 1
        while dest.exists():
            if dest.stat().st_size == st.st_size and _head_equal(dest, p):
                break  # 同一内容とみなしてスキップ
            dest = dest_dir / f"{p.stem}_{n}{p.suffix}"
            n += 1
        if dest.exists():
            skipped += 1
            continue
        if not dry_run:
            shutil.copy2(p, dest)
            register_file(dest, conn)
            conn.commit()
        copied += 1
    print(f"import: {copied} copied, {skipped} skipped (duplicate)")
    return {"total": len(files), "copied": copied, "skipped": skipped,
            "src": str(src), "dry_run": dry_run}


# ---------------------------------------------------------------------------
# upload: ドラッグ＆ドロップ / ファイル選択からの取り込み
# ---------------------------------------------------------------------------

def sanitize_filename(name: str) -> str:
    """アップロードされたファイル名を安全なものにする。"""
    name = os.path.basename(name.replace("\\", "/")).strip()
    name = "".join(c for c in name if c not in '\x00/:;*?"<>|')
    return name or "upload"


def save_upload(name: str, fh, fallback_ts: float | None = None) -> tuple[Path, bool]:
    """アップロードを Exif 日付（無ければ fallback_ts）で日付フォルダへ保存する。

    同名がある場合は内容比較し、同一ならスキップ（dest を返す）、
    違えば _1, _2... 連番で保存。
    戻り値は (保存先, 重複スキップならTrue)。
    """
    name = sanitize_filename(name)
    tmp = common.PHOTO_DIR / ".upload-tmp"
    tmp.mkdir(parents=True, exist_ok=True)
    fd, tmpname = tempfile.mkstemp(dir=tmp, suffix=".part")
    dest_tmp = Path(tmpname)
    try:
        with os.fdopen(fd, "wb") as out:
            shutil.copyfileobj(fh, out, length=4 * 1024 * 1024)
    except Exception:
        dest_tmp.unlink(missing_ok=True)
        raise

    try:
        # Exif の壁時計をそのままフォルダ分割に使う。
        # extract の戻り値は壁時計に UTC ラベルを付けたものなので、
        # そのまま astimezone() すると JST で +9 時間ずれて日付が翌日になる。
        _, d_loc = capture_dates(dest_tmp, fallback_ts=fallback_ts)
        ts = None if exif.extract_capture_datetime(dest_tmp) is not None else (
            fallback_ts if fallback_ts is not None else dest_tmp.stat().st_mtime
        )
        ymd = f"{d_loc.year:04d}{d_loc.month:02d}{d_loc.day:02d}"
        dest_dir = (common.PHOTO_DIR / f"{d_loc.year:04d}"
                    / f"{d_loc.year:04d}{d_loc.month:02d}" / f"{ymd}_")
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / name
        n = 1
        while dest.exists():
            if dest.stat().st_size == dest_tmp.stat().st_size:
                with dest.open("rb") as a, dest_tmp.open("rb") as b:
                    if a.read(65536) == b.read(65536):
                        break  # 同一内容とみなす
            dest = dest_dir / f"{Path(name).stem}_{n}{Path(name).suffix}"
            n += 1
        if dest.exists():
            dest_tmp.unlink()  # 重複: 破棄して既存パスを返す
            return dest, True
        else:
            dest_tmp.rename(dest)
            if ts is not None:
                os.utime(dest, (ts, ts))  # Exif 無しファイルは mtime も合わせる
        return dest, False
    except Exception:
        dest_tmp.unlink(missing_ok=True)
        raise


def finalize_upload(path: Path, fallback_ts: float | None = None) -> int | None:
    """保存済みファイルを DB 登録し、サムネイルを即時生成する。photo id を返す。"""
    conn = common.get_db()
    if register_file(path, conn, fallback_ts=fallback_ts):
        conn.commit()
    row = conn.execute("SELECT id FROM photos WHERE path=?",
                       (path.relative_to(common.PHOTO_DIR).as_posix(),)).fetchone()
    if row is None:
        return None
    thumb_row = conn.execute("SELECT * FROM photos WHERE id=?", (row["id"],)).fetchone()
    if thumb_row and thumb_row["thumb_done"] == 0:
        make_thumbnail(thumb_row)
    return row["id"]


def main(argv: list[str]) -> None:
    if len(argv) >= 1 and argv[0] == "scan":
        scan_data_dir()
    elif len(argv) >= 1 and argv[0] == "thumbs":
        limit = int(argv[1]) if len(argv) >= 2 else None
        process_thumbnails(limit)
    elif len(argv) >= 1 and argv[0] == "views":
        limit = int(argv[1]) if len(argv) >= 2 else None
        process_views(limit)
    elif len(argv) >= 2 and argv[0] == "import":
        try:
            import_source(argv[1], dry_run=("--dry-run" in argv))
        except ValueError as e:
            print(str(e), file=sys.stderr)
            sys.exit(1)
    else:
        print(__doc__)


if __name__ == "__main__":
    main(sys.argv[1:])
