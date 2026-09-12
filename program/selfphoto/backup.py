"""バックアップ: rsync による photo 配下のコピー。

設定は DATA_DIR/backup.json（リポジトリ外・600 パーミッション）。
保存のたびに rsync 全体走査すると重いため、監視モードは
dirty フラグ＋間隔実行（rsyncgui の intervals と同じ方式）。
アップロード等で dirty が立ち、ワーカースレッドが間隔ごとに
dirty のときだけ rsync を実行する。手動の「コピー実行」はいつでも可。

固定オプション: -r -t -u -v --progress（除外: .upload-tmp/）。
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import threading
import time
from pathlib import Path

from . import common

BACKUP_FILE = common.DATA_DIR / "backup.json"

# rsync 固定オプション（-r 再帰 / -t 時刻維持 / -u 新しいもののみ / -v 詳細 / --progress 進捗）
FIXED_OPTS = ["-r", "-t", "-u", "-v", "--progress"]
EXCLUDE_UPLOAD_TMP = ".upload-tmp/"
WATCH_INTERVALS = [60, 300, 900, 1800, 3600]
DEFAULT_INTERVAL = 300

# ローカル転送先として許可する場所。selfphoto-server.service の
# ReadWritePaths と一致させること（PrivateTmp のため /tmp 等は不可）。
# サービスのプライベート名前空間に書いて「成功」扱いになるのを防ぐ。
WRITABLE_ROOTS = [common.DATA_DIR, Path("/mnt"), Path("/media"), Path("/run/media")]


def _inside_writable(path: Path) -> bool:
    try:
        rp = path.resolve()
    except OSError:
        return False
    for root in WRITABLE_ROOTS:
        try:
            rr = root.resolve()
        except OSError:
            continue
        if rp == rr or rr in rp.parents:
            return True
    return False

_lock = threading.Lock()
_run_lock = threading.Lock()
_running = False
_dirty = False
_last_run: dict = {}
_watch_thread: threading.Thread | None = None
_watch_stop: threading.Event | None = None


def default_config() -> dict:
    return {
        "source": str(common.PHOTO_DIR),
        "target": "",
        "ssh": {"enabled": False, "host": "", "user": "",
                "port": "22", "key": "", "password": ""},
        "watch": {"enabled": False, "intervalSec": DEFAULT_INTERVAL},
        "lastRun": None,
    }


def load_config() -> dict:
    cfg = default_config()
    try:
        if BACKUP_FILE.is_file():
            with BACKUP_FILE.open(encoding="utf-8") as f:
                saved = json.load(f)
            if isinstance(saved, dict):
                cfg.update({k: v for k, v in saved.items() if k in cfg})
                if isinstance(saved.get("ssh"), dict):
                    cfg["ssh"].update({k: v for k, v in saved["ssh"].items()
                                       if k in cfg["ssh"]})
                if isinstance(saved.get("watch"), dict):
                    cfg["watch"].update({k: v for k, v in saved["watch"].items()
                                         if k in cfg["watch"]})
    except Exception:
        pass
    return cfg


def save_config(cfg: dict) -> None:
    common.DATA_DIR.mkdir(parents=True, exist_ok=True)
    tmp = BACKUP_FILE.with_name(BACKUP_FILE.name + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2, ensure_ascii=False)
    os.replace(tmp, BACKUP_FILE)
    try:
        os.chmod(BACKUP_FILE, 0o600)
    except OSError:
        pass


def validate_config(cfg: dict) -> str | None:
    """不正なら理由を返す。OK なら None。"""
    src = (cfg.get("source") or "").strip()
    if not src:
        return "source required"
    sp = Path(src)
    if not sp.is_absolute():
        return "source must be absolute"
    try:
        # 写真データ配下に限定（.. 脱出もここで排除）
        sp.resolve().relative_to(common.DATA_DIR.resolve())
    except (ValueError, OSError):
        return "source must be inside data dir"
    if not sp.is_dir():
        return "source not found"
    tgt = (cfg.get("target") or "").strip()
    if not tgt:
        return "target required"
    ssh = cfg.get("ssh") or {}
    if ssh.get("enabled"):
        if not (ssh.get("host") or "").strip():
            return "ssh host required"
        # remote target は user@host:path 形式も素の path も可（後者は自動付与）
    else:
        if not Path(tgt).is_absolute():
            return "target must be absolute"
        if not _inside_writable(Path(tgt)):
            roots = ", ".join([str(common.DATA_DIR), "/mnt", "/media", "/run/media"])
            return f"target not writable here (use {roots}, or SSH)"
    iv = (cfg.get("watch") or {}).get("intervalSec", DEFAULT_INTERVAL)
    try:
        iv = int(iv)
    except (TypeError, ValueError):
        return "bad interval"
    if iv not in WATCH_INTERVALS:
        return "bad interval"
    return None


def build_command(cfg: dict) -> tuple[list[str], dict[str, str], str]:
    """(argv, env追加分, 表示用コマンド) を返す。shell は使わない。"""
    src = (cfg["source"] or "").rstrip("/") + "/"
    tgt = (cfg["target"] or "").strip()
    ssh = cfg.get("ssh") or {}
    argv = ["rsync"] + FIXED_OPTS + [f"--exclude={EXCLUDE_UPLOAD_TMP}"]
    env_add: dict[str, str] = {}
    if ssh.get("enabled") and (ssh.get("host") or "").strip():
        parts = ["ssh", "-o", "StrictHostKeyChecking=no"]
        port = (ssh.get("port") or "22").strip() or "22"
        if port != "22":
            parts += ["-p", port]
        key = (ssh.get("key") or "").strip()
        if key:
            parts += ["-i", key]
        argv += ["-e", " ".join(parts)]
        user = (ssh.get("user") or "").strip()
        prefix = f"{user}@" if user else ""
        if ":" not in tgt:
            tgt = f"{prefix}{ssh['host'].strip()}:{tgt}"
        password = ssh.get("password") or ""
        if password and shutil.which("sshpass"):
            argv = ["sshpass", "-e"] + argv
            env_add["SSHPASS"] = password
    argv += [src, tgt]
    return argv, env_add, " ".join(argv)


SSH_TIMEOUT = 20


def _ssh_dest(ssh: dict) -> str:
    host = (ssh.get("host") or "").strip()
    user = (ssh.get("user") or "").strip()
    return f"{user}@{host}" if user else host


def _ssh_base_args(ssh: dict) -> list[str]:
    args = ["ssh", "-o", "StrictHostKeyChecking=no",
            "-o", "ConnectTimeout=10"]
    port = (str(ssh.get("port") or "22")).strip() or "22"
    if port != "22":
        args += ["-p", port]
    key = (ssh.get("key") or "").strip()
    if key:
        args += ["-i", key]
    return args


def _ssh_env_add(ssh: dict) -> dict[str, str]:
    password = ssh.get("password") or ""
    if password and shutil.which("sshpass"):
        return {"SSHPASS": password}
    return {}


def _ssh_prefix(ssh: dict) -> list[str]:
    if _ssh_env_add(ssh):
        return ["sshpass", "-e"]
    return []


def parse_remote_path(tgt: str, ssh: dict) -> str:
    """ターゲット文字列からリモート側パスだけ取り出す。
    user@host:path 形式にも素の path にも対応する。"""
    t = (tgt or "").strip()
    if ":" in t:
        _, _, rp = t.rpartition(":")
        return rp.strip() or t
    return t


def test_ssh_connection(ssh: dict) -> dict:
    """SSH 接続だけ確認する。成功なら {"ok": True}、失敗なら理由付きで返す。"""
    host = ((ssh or {}).get("host") or "").strip()
    if not host:
        return {"ok": False, "error": "ssh host required"}
    if shutil.which("ssh") is None:
        return {"ok": False, "error": "ssh not found"}
    dest = _ssh_dest(ssh)
    argv = _ssh_prefix(ssh) + _ssh_base_args(ssh) + [dest, "echo", "ok"]
    env = dict(os.environ)
    env.update(_ssh_env_add(ssh))
    try:
        proc = subprocess.run(argv, capture_output=True, text=True,
                              timeout=SSH_TIMEOUT, env=env)
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "ssh connection timed out"}
    except FileNotFoundError:
        return {"ok": False, "error": "ssh not found"}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": str(e)}
    if proc.returncode == 0 and proc.stdout.strip().endswith("ok"):
        return {"ok": True, "message": "SSH接続OK"}
    log = ((proc.stdout or "") + (proc.stderr or "")).strip()
    return {"ok": False, "error": f"ssh connection failed (exit {proc.returncode}): {log[:2000]}"}


def check_target(target: str, ssh: dict | None = None, create: bool = True) -> dict:
    """ターゲットフォルダの存在確認。無い場合は作成する（mkdir -p）。
    成功なら {"ok": True, "created": bool} を返す。"""
    ssh = ssh or {}
    tgt = (target or "").strip()
    if not tgt:
        return {"ok": False, "error": "target required"}
    use_ssh = bool(ssh.get("enabled") and (ssh.get("host") or "").strip())
    if use_ssh:
        remote = parse_remote_path(tgt, ssh)
        if not remote or not remote.startswith(("/", "~")):
            return {"ok": False, "error": "remote target must be absolute path"}
        if shutil.which("ssh") is None:
            return {"ok": False, "error": "ssh not found"}
        dest = _ssh_dest(ssh)
        env = dict(os.environ)
        env.update(_ssh_env_add(ssh))

        def _run_remote(*remote_cmd: str) -> subprocess.CompletedProcess:
            return subprocess.run(
                _ssh_prefix(ssh) + _ssh_base_args(ssh) + [dest, *remote_cmd],
                capture_output=True, text=True, timeout=SSH_TIMEOUT, env=env)

        try:
            # 先に存在確認（作成したかどうかの表示用）
            pre = _run_remote("test", "-d", remote)
            existed = (pre.returncode == 0)
            if not existed and create:
                mk = _run_remote("mkdir", "-p", remote)
                if mk.returncode != 0:
                    log = ((mk.stdout or "") + (mk.stderr or "")).strip()
                    return {"ok": False, "error": f"mkdir remote failed: {log[:2000]}"}
            post = _run_remote("test", "-d", remote)
            if post.returncode != 0:
                log = ((post.stdout or "") + (post.stderr or "")).strip()
                return {"ok": False, "error": f"remote target not found: {log[:2000]}"}
            return {"ok": True, "created": (not existed),
                    "message": "フォルダOK（既存）" if existed else "フォルダを作成しました"}
        except subprocess.TimeoutExpired:
            return {"ok": False, "error": "ssh connection timed out"}
        except FileNotFoundError:
            return {"ok": False, "error": "ssh not found"}
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "error": str(e)}
    # ローカル
    p = Path(tgt)
    if not p.is_absolute():
        return {"ok": False, "error": "target must be absolute"}
    existed = p.is_dir()
    if not existed and create:
        try:
            p.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            return {"ok": False, "error": f"mkdir target failed: {e}"}
    if not p.is_dir():
        return {"ok": False, "error": "target not found"}
    if not os.access(p, os.W_OK | os.X_OK):
        return {"ok": False, "error": "target not writable"}
    return {"ok": True, "created": (not existed),
            "message": "フォルダOK（既存）" if existed else "フォルダを作成しました"}


def rsync_available() -> bool:
    return shutil.which("rsync") is not None


def _finish_run(ok: bool, exit_code: int, log: str, started: float, detail: str = "") -> dict:
    global _last_run
    with _lock:
        _last_run = {
            "ok": ok, "exitCode": exit_code,
            "startedAt": started, "finishedAt": time.time(),
            "logTail": log[-8000:], "detail": detail,
        }
        cfg = load_config()
        cfg["lastRun"] = _last_run
        try:
            save_config(cfg)
        except Exception:
            pass
        return dict(_last_run)


def run_once(detail: str = "manual") -> dict:
    """rsync を1回実行する（重複実行は抑止）。結果 dict を返す。"""
    global _running, _dirty
    with _run_lock:
        if _running:
            return {"ok": False, "alreadyRunning": True}
        _running = True
    started = time.time()
    try:
        if not rsync_available():
            return _finish_run(False, -1, "rsync not found", started, detail)
        cfg = load_config()
        err = validate_config(cfg)
        if err:
            return _finish_run(False, -1, f"invalid config: {err}", started, detail)
        argv, env_add, _display = build_command(cfg)
        # ターゲットが無い場合は作成する（ローカルは mkdir -p、
        # SSH 先はリモートで mkdir -p）。ここで失敗したら接続エラーと
        # 区別できるようメッセージを付ける。
        ssh = cfg.get("ssh") or {}
        chk = check_target(cfg.get("target") or "", ssh, create=True)
        if not chk.get("ok"):
            return _finish_run(False, -1, f"target check failed: {chk.get('error')}", started, detail)
        env = dict(os.environ)
        env.update(env_add)
        try:
            proc = subprocess.run(argv, capture_output=True, text=True,
                                  timeout=6 * 3600, env=env)
            log = (proc.stdout or "") + (proc.stderr or "")
            with _lock:
                _dirty = False
            return _finish_run(proc.returncode == 0, proc.returncode, log, started, detail)
        except subprocess.TimeoutExpired:
            return _finish_run(False, -1, "timeout (6h)", started, detail)
        except FileNotFoundError:
            return _finish_run(False, -1, "rsync not found", started, detail)
    finally:
        with _run_lock:
            _running = False


def run_async(detail: str = "manual") -> dict:
    """バックグラウンドで実行開始する。"""
    with _run_lock:
        if _running:
            return {"ok": False, "alreadyRunning": True}
    t = threading.Thread(target=run_once, args=(detail,), daemon=True)
    t.start()
    return {"ok": True, "started": True}


def mark_dirty() -> None:
    """アップロード等で写真が増えたことを記録する（監視スレッドが拾う）。"""
    global _dirty
    with _lock:
        _dirty = True


def status() -> dict:
    with _lock:
        running = _running
        dirty = _dirty
        last = dict(_last_run) if _last_run else load_config().get("lastRun")
    cfg = load_config()
    return {
        "rsyncAvailable": rsync_available(),
        "running": running,
        "dirty": dirty,
        "watch": cfg.get("watch"),
        "source": cfg.get("source"),
        "target": cfg.get("target"),
        "lastRun": last,
    }


def _current_interval() -> int:
    try:
        iv = int(load_config()["watch"].get("intervalSec", DEFAULT_INTERVAL))
    except (TypeError, ValueError):
        return DEFAULT_INTERVAL
    return iv if iv in WATCH_INTERVALS else DEFAULT_INTERVAL


def _watch_loop() -> None:
    global _watch_stop
    stop = _watch_stop
    while stop is not None and not stop.wait(_current_interval()):
        with _lock:
            dirty = _dirty
        if dirty:
            run_once(detail="watch")


def set_watch(enabled: bool) -> dict:
    """監視スレッドの開始・停止。"""
    global _watch_thread, _watch_stop
    with _lock:
        if enabled:
            if _watch_thread is not None and _watch_thread.is_alive():
                return {"ok": True, "watching": True}
            _watch_stop = threading.Event()
            _watch_thread = threading.Thread(target=_watch_loop, daemon=True)
            _watch_thread.start()
            return {"ok": True, "watching": True}
        else:
            if _watch_stop is not None:
                _watch_stop.set()
            _watch_thread = None
            return {"ok": True, "watching": False}


def is_watching() -> bool:
    with _lock:
        return _watch_thread is not None and _watch_thread.is_alive()


def ensure_watch() -> None:
    """起動時に設定を見て監視を復帰する。"""
    try:
        if load_config().get("watch", {}).get("enabled"):
            set_watch(True)
    except Exception:
        pass
