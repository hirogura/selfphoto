"""バックアップ: rsync による photo 配下のコピー。

設定は DATA_DIR/backup.json（リポジトリ外・600 パーミッション）。
保存のたびに rsync 全体走査すると重いため、監視モードは
dirty フラグ＋間隔実行（rsyncgui の intervals と同じ方式）。
アップロード等で dirty が立ち、ワーカースレッドが間隔ごとに
dirty のときだけ rsync を実行する。手動の「コピー実行」はいつでも可。

監視モードは rsyncgui と同じ形で2種類:
  - interval: WATCH_INTERVALS の間隔ごとに dirty なら実行
  - time: 指定曜日・指定時刻（times は "HH:MM" の複数可）に
    dirty なら実行（夜間実行用。cron と同じく分単位の判定）。

固定オプション: -r -t -u -v --progress（除外: .upload-tmp/）。
ミラーリング有効時は --delete を追加し、ソースに無いファイルを
ターゲット側から削除する（既定は Off）。
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
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
# 指定時刻モードの既定値（rsyncgui と同じ形: times は "HH:MM" 複数可、
# days は 0=日〜6=土）。
WATCH_MODES = ("interval", "time")
DEFAULT_TIMES = ["02:00"]
DEFAULT_DAYS = [0, 1, 2, 3, 4, 5, 6]
# time モードのポーリング間隔（分境界を取りこぼさないよう短めに固定。
# 保存・起動直後は初回待ちなしに即時判定も行うため、対象分内の保存でも
# 実行機会を逃さない）
WATCH_TIME_POLL_SEC = 15

# ローカル転送先として許可する場所。selfphoto-server.service の
# ReadWritePaths と一致させること（PrivateTmp のため /tmp 等は不可）。
# サービスのプライベート名前空間に書いて「成功」扱いになるのを防ぐ。
# /opt/lxd-data はデータルート（selfphoto-data の兄弟に photo-back 等を
# 作る想定）のため許可する。
WRITABLE_ROOTS = [common.DATA_DIR, Path("/opt/lxd-data"), Path("/mnt"), Path("/media"), Path("/run/media")]


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
        "mirror": False,
        "ssh": {"enabled": False, "host": "", "user": "",
                "port": "22", "key": "", "password": ""},
        "watch": {"enabled": False, "mode": "interval",
                  "intervalSec": DEFAULT_INTERVAL,
                  "times": list(DEFAULT_TIMES), "days": list(DEFAULT_DAYS)},
        "lastRun": None,
    }


def _parse_time_str(s: str) -> tuple[int, int] | None:
    """rsyncgui と同じ "HH:MM" 形式を (hour, minute) に変換する。"""
    try:
        h_s, _, m_s = str(s).strip().partition(":")
        h, m = int(h_s), int(m_s)
    except (TypeError, ValueError):
        return None
    if 0 <= h <= 23 and 0 <= m <= 59:
        return h, m
    return None


def normalize_watch(w: dict | None) -> dict:
    """watch 設定を正規化する（旧設定の救済つき）。

    rsyncgui と同じ形: {enabled, mode, intervalSec, times, days}。
    mode 無しの旧設定は interval 扱い。
    """
    src = w if isinstance(w, dict) else {}
    mode = src.get("mode", "interval")
    if mode not in WATCH_MODES:
        mode = "interval"
    try:
        iv = int(src.get("intervalSec", DEFAULT_INTERVAL))
    except (TypeError, ValueError):
        iv = DEFAULT_INTERVAL
    if iv not in WATCH_INTERVALS:
        iv = DEFAULT_INTERVAL
    times: list[str] = []
    raw_times = src.get("times", DEFAULT_TIMES)
    if not isinstance(raw_times, list):
        raw_times = [raw_times]
    for t in raw_times:
        if _parse_time_str(t) is not None:
            hh, mm = _parse_time_str(t)  # type: ignore[misc]
            times.append(f"{hh:02d}:{mm:02d}")
        if len(times) >= 10:
            break
    if not times:
        times = list(DEFAULT_TIMES)
    days: list[int] = []
    raw_days = src.get("days", DEFAULT_DAYS)
    if isinstance(raw_days, list):
        for d in raw_days:
            try:
                di = int(d)
            except (TypeError, ValueError):
                continue
            if 0 <= di <= 6 and di not in days:
                days.append(di)
    if not days:
        days = list(DEFAULT_DAYS)
    days.sort()
    return {"enabled": bool(src.get("enabled", False)), "mode": mode,
            "intervalSec": iv, "times": times, "days": days}


def load_config() -> dict:
    cfg = default_config()
    try:
        if BACKUP_FILE.is_file():
            with BACKUP_FILE.open(encoding="utf-8") as f:
                saved = json.load(f)
            if isinstance(saved, dict):
                cfg.update({k: v for k, v in saved.items() if k in cfg})
                cfg["mirror"] = bool(saved.get("mirror", False))
                if isinstance(saved.get("ssh"), dict):
                    cfg["ssh"].update({k: v for k, v in saved["ssh"].items()
                                       if k in cfg["ssh"]})
                if isinstance(saved.get("watch"), dict):
                    merged = dict(cfg["watch"])
                    merged.update({k: v for k, v in saved["watch"].items()
                                   if k in ("enabled", "mode", "intervalSec",
                                            "times", "days")})
                    cfg["watch"] = normalize_watch(merged)
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
            roots = ", ".join([str(common.DATA_DIR), "/opt/lxd-data", "/mnt", "/media", "/run/media"])
            return f"target not writable here (use {roots}, or SSH)"
    iv = (cfg.get("watch") or {}).get("intervalSec", DEFAULT_INTERVAL)
    try:
        iv = int(iv)
    except (TypeError, ValueError):
        return "bad interval"
    if iv not in WATCH_INTERVALS:
        return "bad interval"
    w = normalize_watch(cfg.get("watch"))
    if w["mode"] == "time":
        if not w["times"]:
            return "bad time"
        for t in w["times"]:
            if _parse_time_str(t) is None:
                return "bad time"
        if not w["days"] or any(d not in range(7) for d in w["days"]):
            return "bad days"
    return None


def build_command(cfg: dict) -> tuple[list[str], dict[str, str], str]:
    """(argv, env追加分, 表示用コマンド) を返す。shell は使わない。"""
    src = (cfg["source"] or "").rstrip("/") + "/"
    tgt = (cfg["target"] or "").strip()
    ssh = cfg.get("ssh") or {}
    argv = ["rsync"] + FIXED_OPTS + [f"--exclude={EXCLUDE_UPLOAD_TMP}"]
    if cfg.get("mirror"):
        argv += ["--delete"]
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


def ssh_diagnostics(ssh: dict) -> dict:
    """接続確認用の診断情報（パスワード自体は含めない）。

    ターミナルでは繋がるのにUIでは失敗する原因の多くは、
    「サーバープロセス(root)とターミナルのユーザーが別で、
    鍵・~/.ssh/config・ssh-agentを共有していない」ことにある。
    その切り分け用に実行ユーザー・鍵・sshpassの状態を返す。
    """
    import getpass
    ssh = ssh or {}
    try:
        run_user = getpass.getuser()
    except Exception:
        run_user = f"uid={os.geteuid()}"
    home = os.path.expanduser("~")
    key = ((ssh.get("key") or "").strip())
    key_info: dict = {"specified": bool(key)}
    if key:
        kp = Path(key)
        key_info.update({
            "path": key,
            "exists": kp.is_file(),
            "readable": os.access(key, os.R_OK),
        })
    # 鍵ファイル未指定時は ssh が使うデフォルト鍵の有無を見る
    default_keys = []
    for name in ("id_ed25519", "id_ecdsa", "id_rsa"):
        p = Path(home) / ".ssh" / name
        try:
            if p.is_file():
                default_keys.append(name)
        except OSError:
            pass
    return {
        "runUser": run_user,
        "home": home,
        "sshpassAvailable": shutil.which("sshpass") is not None,
        "passwordSet": bool(ssh.get("password")),
        "key": key_info,
        "defaultKeys": default_keys,
    }


def _ssh_auth_hint(ssh: dict, diag: dict) -> str:
    """Permission denied 系のときの対処ヒント（日本語）。"""
    hints = []
    if diag.get("passwordSet") and not diag.get("sshpassAvailable"):
        hints.append(
            "パスワードが入力されていますが、サーバーに sshpass が無いため"
            "パスワード認証に使われていません"
            "(apt install sshpass が必要)。")
    key = diag.get("key") or {}
    if key.get("specified"):
        if not key.get("exists"):
            hints.append(
                f"鍵ファイル {key.get('path')} がサーバー上に存在しません"
                "(サーバーから見える絶対パスを指定してください)。")
        elif not key.get("readable"):
            hints.append(
                f"鍵ファイル {key.get('path')} が読み取れません(パーミッションを確認)。")
    elif not diag.get("defaultKeys"):
        hints.append(
            f"サーバー実行ユーザー({diag.get('runUser')})の {diag.get('home')}/.ssh に"
            "秘密鍵が無く、鍵ファイルも未指定です。"
            "ターミナルの鍵は別ユーザーのものなのでサーバーからは見えません。"
            "鍵認証ならサーバー用の鍵を作って転送先に登録するか、"
            "鍵ファイルに絶対パスを指定してください。")
    if not hints:
        hints.append(
            "転送先の authorized_keys(公開鍵の登録)・ユーザー名・パスワードを確認してください。")
    return " ".join(hints)


def test_ssh_connection(ssh: dict) -> dict:
    """SSH 接続だけ確認する。成功なら {"ok": True}、失敗なら理由付きで返す。"""
    host = ((ssh or {}).get("host") or "").strip()
    if not host:
        return {"ok": False, "error": "ssh host required"}
    if shutil.which("ssh") is None:
        return {"ok": False, "error": "ssh not found"}
    diag = ssh_diagnostics(ssh)
    # パスワードがあるのに sshpass が無い場合は認証前に明示する
    # (無いとパスワードが無視されて Permission denied になるため)。
    if diag.get("passwordSet") and not diag.get("sshpassAvailable"):
        return {"ok": False,
                "error": ("パスワード認証できません: "
                          "パスワードが入力されていますが、サーバーに sshpass が"
                          "インストールされていないため無視されます。"
                          "apt install sshpass で導入するか、鍵認証を使ってください。"),
                "diagnostics": diag}
    dest = _ssh_dest(ssh)
    argv = _ssh_prefix(ssh) + _ssh_base_args(ssh) + [dest, "echo", "ok"]
    env = dict(os.environ)
    env.update(_ssh_env_add(ssh))
    try:
        proc = subprocess.run(argv, capture_output=True, text=True,
                              timeout=SSH_TIMEOUT, env=env)
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "ssh connection timed out",
                "diagnostics": diag}
    except FileNotFoundError:
        return {"ok": False, "error": "ssh not found", "diagnostics": diag}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": str(e), "diagnostics": diag}
    if proc.returncode == 0 and proc.stdout.strip().endswith("ok"):
        return {"ok": True, "message": "SSH接続OK", "diagnostics": diag}
    log = ((proc.stdout or "") + (proc.stderr or "")).strip()
    err = f"ssh connection failed (exit {proc.returncode}): {log[:2000]}"
    # 認証失敗なら対処ヒントを付ける（接続エラーなのか設定ミスなのか分かるように）
    if proc.returncode == 255 or "Permission denied" in log:
        err += " [ヒント: " + _ssh_auth_hint(ssh, diag) + "]"
    return {"ok": False, "error": err, "diagnostics": diag}


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


def sshpass_available() -> bool:
    return shutil.which("sshpass") is not None


def install_sshpass(timeout: int = 180) -> dict:
    """sshpass をパッケージマネージャで導入する（サーバー側で実行）。

    既に入っていれば {"ok": True, "already": True} を返す。
    対応マネージャが無い・導入失敗時は {"ok": False, "error": ...} を返す。
    """
    if sshpass_available():
        return {"ok": True, "already": True, "message": "sshpass は既にインストール済みです"}
    managers: list[tuple[str, list[list[str]]]] = [
        ("apt-get", [["apt-get", "update", "-qq"],
                     ["apt-get", "install", "-y", "-qq", "sshpass"]]),
        ("dnf", [["dnf", "install", "-y", "sshpass"]]),
        ("yum", [["yum", "install", "-y", "sshpass"]]),
        ("apk", [["apk", "add", "--no-cache", "sshpass"]]),
        ("pacman", [["pacman", "-Sy", "--noconfirm", "sshpass"]]),
        ("zypper", [["zypper", "--non-interactive", "install", "sshpass"]]),
    ]
    tried: list[str] = []
    logs: list[str] = []
    for mgr, cmds in managers:
        if shutil.which(mgr) is None:
            continue
        tried.append(mgr)
        ok_all = True
        for cmd in cmds:
            try:
                proc = subprocess.run(cmd, capture_output=True, text=True,
                                      timeout=timeout)
            except subprocess.TimeoutExpired:
                return {"ok": False,
                        "error": f"sshpass のインストールがタイムアウトしました ({' '.join(cmd)})",
                        "log": "\n".join(logs)[-4000:]}
            except FileNotFoundError:
                ok_all = False
                break
            except Exception as e:  # noqa: BLE001
                return {"ok": False, "error": str(e),
                        "log": "\n".join(logs)[-4000:]}
            out = ((proc.stdout or "") + (proc.stderr or "")).strip()
            if out:
                logs.append(f"$ {' '.join(cmd)}\n{out[-2000:]}")
            if proc.returncode != 0:
                ok_all = False
                logs.append(f"exit {proc.returncode}: {' '.join(cmd)}")
                break
        if ok_all and sshpass_available():
            return {"ok": True, "already": False,
                    "message": "sshpass をインストールしました",
                    "log": "\n".join(logs)[-4000:]}
        # このマネージャでは失敗 → 次の候補があれば試す
    if not tried:
        return {"ok": False,
                "error": "対応するパッケージマネージャが見つかりません（手動で sshpass を導入してください）",
                "log": "\n".join(logs)[-4000:]}
    if sshpass_available():
        return {"ok": True, "already": False,
                "message": "sshpass をインストールしました",
                "log": "\n".join(logs)[-4000:]}
    tail = ("\n".join(logs)[-4000:] or
            "インストールに失敗しました（ログなし）")
    return {"ok": False,
            "error": f"sshpass のインストールに失敗しました: {tail[-1000:]}",
            "log": tail}


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
    except Exception as e:  # noqa: BLE001
        # 予期せぬ例外を握りつぶさず失敗として記録する。
        # 握りつぶすと監視スレッドが死んで enabled のまま黙って止まるため。
        return _finish_run(False, -1, f"error: {e}", started, detail)
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
        "mirror": bool(cfg.get("mirror", False)),
        "lastRun": last,
    }


def _current_interval() -> int:
    try:
        iv = int(normalize_watch(load_config().get("watch")).get(
            "intervalSec", DEFAULT_INTERVAL))
    except (TypeError, ValueError):
        return DEFAULT_INTERVAL
    return iv if iv in WATCH_INTERVALS else DEFAULT_INTERVAL


def _watch_mode() -> str:
    try:
        return normalize_watch(load_config().get("watch")).get("mode", "interval")
    except Exception:
        return "interval"


def _time_matches(now_hm: str, now_wday: int, times: list[str], days: list[int]) -> bool:
    """rsyncgui の cron 条件と同じ判定（曜日+時刻が一致したら True）。

    now_hm は "HH:MM"、now_wday は 0=日〜6=土。
    """
    return now_wday in days and now_hm in times


def _photo_max_id() -> int | None:
    """photos テーブルの MAX(id)。dirty フラグに加えて件数変化も見ることで、
    Web 以外（スキャンタイマー・CLI 取込）で増えた分も監視コピーするため。"""
    try:
        row = common.get_db().execute("SELECT MAX(id) AS m FROM photos").fetchone()
        return int(row["m"]) if row and row["m"] is not None else 0
    except Exception:
        return None


def _watch_loop() -> None:
    global _watch_stop
    stop = _watch_stop
    # スレッド開始時点を基準にし、再起動直後の不要なコピーは避ける
    baseline = _photo_max_id()
    last_fired_minute: str | None = None

    def _fire(detail: str) -> dict:
        """dirty/grown 時の rsync 実行（発火・結果をサーバログにも残す）。"""
        nonlocal baseline
        print(f"backup watch: firing ({detail})", file=sys.stderr)
        r = run_once(detail=detail)
        if r.get("ok"):
            baseline = _photo_max_id()
        else:
            tail = (r.get("logTail") or "")[-500:]
            print(f"backup watch: failed ({detail}): {tail}",
                  file=sys.stderr)
        return r

    def _tick() -> None:
        nonlocal baseline, last_fired_minute
        w = normalize_watch(load_config().get("watch"))
        if not w.get("enabled"):
            return
        if w["mode"] == "time":
            # 指定時刻モード:
            # 曜日・時刻が一致した分の最初の判定でのみ発火させる。
            # dirty/grown が無ければ rsync 自体は走らせない（無駄な走査を避ける）。
            from datetime import datetime
            now = datetime.now().astimezone()
            hm = f"{now.hour:02d}:{now.minute:02d}"
            wday = (now.weekday() + 1) % 7  # 月曜=0 → 日曜=0 換算
            if not _time_matches(hm, wday, w["times"], w["days"]):
                return
            day_min = now.strftime("%Y-%m-%d %H:%M")
            if last_fired_minute == day_min:
                return
            last_fired_minute = day_min
            with _lock:
                dirty = _dirty
            cur = _photo_max_id()
            grown = (cur is not None and baseline is not None and cur > baseline)
            shrunk = (cur is not None and baseline is not None and cur < baseline)
            mirror = bool(load_config().get("mirror", False))
            if shrunk and not mirror:
                baseline = cur  # 非ミラー時は削除のみ何もしない（--delete 無し）
            if dirty or grown or (mirror and shrunk):
                _fire("watch-time")
            return
        with _lock:
            dirty = _dirty
        cur = _photo_max_id()
        grown = (cur is not None and baseline is not None and cur > baseline)
        shrunk = (cur is not None and baseline is not None and cur < baseline)
        mirror = bool(load_config().get("mirror", False))
        if shrunk and not mirror:
            baseline = cur  # 非ミラー時は削除のみ何もしない（--delete 無し）
        if dirty or grown or (mirror and shrunk):
            _fire("watch")

    # 開始直後に即時判定する。ポーリング待ちだけだと対象分内の保存・再起動で
    # 実行機会を逃して翌日送りになるため（初回ポーリングは +poll 秒後）。
    # 発火済みキー・dirty/grown 判定は通常ポーリングと共通なので二重実行しない。
    try:
        _tick()
    except Exception as e:  # noqa: BLE001
        print(f"backup watch: initial tick failed: {e}", file=sys.stderr)
    while True:
        try:
            w = normalize_watch(load_config().get("watch"))
        except Exception as e:  # noqa: BLE001
            print(f"backup watch: config reload failed: {e}", file=sys.stderr)
            w = {"mode": "interval", "intervalSec": DEFAULT_INTERVAL}
        poll = _current_interval() if w["mode"] == "interval" else WATCH_TIME_POLL_SEC
        if stop is not None and stop.wait(poll):
            break
        if stop is None:
            break
        try:
            _tick()
        except Exception as e:  # noqa: BLE001
            # 予期せぬ例外で監視スレッドが死ぬと enabled のまま黙って止まるため、
            # 失敗をログに残して監視自体は継続する
            print(f"backup watch: tick failed: {e}", file=sys.stderr)


def set_watch(enabled: bool) -> dict:
    """監視スレッドの開始・停止。

    開始時は既存スレッドがあっても作り直す。設定保存で間隔を変えたときに
    古い間隔で眠り続けてコピーされないことがないようにするため。
    """
    global _watch_thread, _watch_stop
    with _lock:
        if enabled:
            old = _watch_thread
            if old is not None and old.is_alive() and _watch_stop is not None:
                _watch_stop.set()
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
