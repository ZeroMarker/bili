#!/usr/bin/env python3
"""Bili 推流管理 WebUI（独立实现，标准库 only，不依赖 tiktok 仓库）。

管两个 systemd user unit（互斥，同时最多跑一个）：
  bili-live.service    直播推流：push.sh <TARGET>（TikTok 直播源 -> Bilibili）
  bili-replay.service  文件轮播：replay.sh <输入>... [--encode]（本地 mp4 -> Bilibili）

另可开关 B 站房间（live.py start/stop/update/status）。

安全：默认只监听 127.0.0.1，无应用层认证；公网发布必须经反代加 Basic Auth。
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import shlex
import tempfile
import threading
from functools import wraps
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

PROJECT_ROOT = Path(__file__).resolve().parent.parent
WEBUI_DIR = Path(__file__).resolve().parent
INDEX_FILE = WEBUI_DIR / "index.html"
LIVE_PY = PROJECT_ROOT / "live.py"
CONFIG_DIR = Path.home() / ".config" / "bili"
LIVE_ENV = CONFIG_DIR / "live.env"
REPLAY_ENV = CONFIG_DIR / "replay.env"
SESSION_FILE = PROJECT_ROOT / ".bilibili_session.json"
PUSH_ENV = CONFIG_DIR / "push.env"
CONTROL_LOCK = threading.Lock()


class ControlBusy(RuntimeError):
    pass


def exclusive(fn):
    @wraps(fn)
    def wrapped(*args, **kwargs):
        if not CONTROL_LOCK.acquire(blocking=False):
            raise ControlBusy("另一项操作正在执行，请完成后重试")
        try:
            return fn(*args, **kwargs)
        finally:
            CONTROL_LOCK.release()
    return wrapped

LIVE_UNIT = "bili-live.service"
REPLAY_UNIT = "bili-replay.service"
WEBUI_UNIT = "bili-webui.service"
MANAGED_UNITS = {LIVE_UNIT, REPLAY_UNIT}
LOGABLE_UNITS = {LIVE_UNIT, REPLAY_UNIT, WEBUI_UNIT}

TARGET_RE = re.compile(r"[A-Za-z0-9_.]{1,64}")
SYSTEMCTL = ["systemctl", "--user"]


def run(argv: list[str], timeout: int = 20, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(argv, text=True, capture_output=True, timeout=timeout, check=check)
def _show(unit: str) -> dict[str, str]:
    r = run([*SYSTEMCTL, "show", unit, "-p", "ActiveState,SubState,MainPID"], check=False)
    props = dict(
        line.split("=", 1) for line in r.stdout.splitlines() if "=" in line
    )
    return {
        "active": props.get("ActiveState", "unknown"),
        "sub": props.get("SubState", "unknown"),
        "pid": int(props.get("MainPID", "0") or 0),
    }


def _has_ffmpeg(root_pid: int) -> bool | None:
    """MainPID 下是否有 ffmpeg 子孙进程（即是否正在推流）。"""
    if not root_pid:
        return False
    try:
        children: dict[int, list[int]] = {}
        comms: dict[int, str] = {}
        for pid_dir in Path("/proc").iterdir():
            if not pid_dir.name.isdigit():
                continue
            pid = int(pid_dir.name)
            try:
                ppid = int((pid_dir / "stat").read_text().split(")", 1)[1].split()[1])
                comms[pid] = (pid_dir / "comm").read_text().strip()
                children.setdefault(ppid, []).append(pid)
            except (OSError, ValueError, IndexError):
                continue
    except OSError:
        return None
    stack = [root_pid]
    while stack:
        cur = stack.pop()
        if comms.get(cur) == "ffmpeg" and cur != root_pid:
            return True
        stack.extend(children.get(cur, []))
    return comms.get(root_pid) == "ffmpeg"


def _read_env(path: Path, key: str) -> str:
    try:
        for line in path.read_text().splitlines():
            if line.startswith(key + "="):
                return line.split("=", 1)[1].strip()
    except OSError:
        pass
    return ""


def _write_env(path: Path, lines: list[str]) -> None:
    CONFIG_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(dir=path.parent, prefix=".bili-")
    try:
        with os.fdopen(fd, "w") as stream:
            stream.write("# 由 webui 写入（600，不进 git）\n" + "".join(l + "\n" for l in lines))
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


def room_status() -> dict[str, object]:
    r = run(["python3", str(LIVE_PY), "status"], timeout=30, check=False)
    out = (r.stdout + r.stderr).strip()
    return {"ok": r.returncode == 0, "text": out[-2000:]}


def sync_push_env() -> None:
    """把最新推流码写入独立的私有配置，不修改用户 shell 配置。"""
    try:
        data = json.loads(SESSION_FILE.read_text())
        url, code = data["rtmp_addr"], data["rtmp_code"]
        if not all(isinstance(v, str) and v and "\n" not in v for v in (url, code)):
            raise ValueError("推流配置为空或格式不正确")
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise RuntimeError("读取会话推流码失败，请重新开播") from exc
    _write_env(PUSH_ENV, [
        f"export BILIBILI_PUSH_URL={shlex.quote(url)}",
        f"export BILIBILI_PUSH_CODE={shlex.quote(code)}",
    ])


def ensure_room_live() -> None:
    """切源前保证 B 站房间开着：已开直接返回；关播则用上次分区/标题自动开播并同步推流码。
    无历史记录时抛错，提示用户去房间卡手动开播。调用方在重启推流单元之前调用，新码即刻生效。"""
    r = run(["python3", str(LIVE_PY), "is-live"], timeout=30, check=False)
    if r.returncode == 0:
        return
    try:
        session = json.loads(SESSION_FILE.read_text())
        area, title = int(session.get("area_id", 0)), str(session.get("title", "")).strip()
    except (OSError, ValueError, TypeError) as exc:
        raise RuntimeError(f"读取上次开播记录失败: {exc}") from exc
    if not area or not title:
        raise ValueError("B 站房间未开播，且没有上次开播记录：请先在房间卡填分区 ID 和标题，点开播")
    r = run(["python3", str(LIVE_PY), "start", "--area", str(area), "--title", title],
            timeout=90, check=False)
    if r.returncode != 0:
        raise RuntimeError((r.stdout + r.stderr).strip()[-2000:] or "关播状态下自动开播失败")
    sync_push_env()


def _ctl(*args: str) -> None:
    r = run([*SYSTEMCTL, *args], timeout=45, check=False)
    if r.returncode != 0:
        raise RuntimeError((r.stderr or r.stdout).strip() or "systemctl 执行失败")


def _wait_inactive(unit: str, timeout: int = 35) -> bool:
    import time
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if _show(unit)["active"] in ("inactive", "failed"):
            return True
        time.sleep(1)
    return False
def probe_tiktok(target: str, timeout: int = 45) -> str:
    """切源前先验流：与 push.sh 同链路（FLV + impersonate + cookies）。
    拿不到地址就抛错，调用方保持现状不动。"""
    cookies = os.environ.get("TK_COOKIES", str(Path.home() / "tiktok" / "cookies.txt"))
    cmd = ["yt-dlp", "--no-warnings", "-f", "b[ext=flv]/best",
           "--impersonate", "chrome"]
    if Path(cookies).is_file():
        cmd += ["--cookies", cookies]
    cmd += ["--get-url", f"https://www.tiktok.com/@{target}/live"]
    r = run(cmd, timeout=timeout, check=False)
    for line in (r.stdout or "").splitlines():
        line = line.strip()
        if line.startswith("http"):
            return line
    raise ValueError(f"@{target} 当前未开播或抓不到流（已保持原推流不动）")




def status() -> dict[str, object]:
    live = _show(LIVE_UNIT)
    replay = _show(REPLAY_UNIT)
    live["pushing"] = _has_ffmpeg(live["pid"])
    replay["pushing"] = _has_ffmpeg(replay["pid"])
    live["target"] = _read_env(LIVE_ENV, "TARGET")
    replay["args"] = _read_env(REPLAY_ENV, "REPLAY_ARGS")
    if live["active"] == "active" and replay["active"] == "active":
        mode = "conflict"
    elif live["active"] == "active":
        mode = "live"
    elif replay["active"] == "active":
        mode = "replay"
    else:
        mode = "idle"
    return {"mode": mode, "live": live, "replay": replay, "room": room_status()}


@exclusive
def set_mode(data: dict) -> dict[str, object]:
    mode = str(data.get("mode", ""))
    if mode == "live":
        target = str(data.get("target", "")).strip()
        if not TARGET_RE.fullmatch(target):
            raise ValueError("TARGET 非法（允许字母数字 . _，≤64 字符）")
        if not bool(data.get("force", False)):
            probe_tiktok(target)  # 先拿到串流地址再停旧起新，失败则保持现状
        ensure_room_live()  # 关播时用上次分区/标题自动开播，否则切了也推不上去
        _write_env(LIVE_ENV, [f"TARGET={target}"])
        _ctl("disable", "--now", REPLAY_UNIT)
        if not _wait_inactive(REPLAY_UNIT):
            raise RuntimeError("文件轮播尚未停止，已取消启动直播推流")
        # 意图性重启不受 crash 熔断计数限制：先清 start-limit，否则连续切换直接 400 且单元变 failed
        run([*SYSTEMCTL, "reset-failed", LIVE_UNIT], check=False)
        _ctl("enable", LIVE_UNIT)
        _ctl("restart", LIVE_UNIT)  # 新 TARGET 只在新进程生效
    elif mode == "replay":
        paths = data.get("paths", [])
        if not isinstance(paths, list) or not paths or len(paths) > 32:
            raise ValueError("paths 需为 1~32 个已存在的文件/目录")
        if any(not isinstance(p, str) for p in paths):
            raise ValueError("paths 含非字符串")
        # 粘贴常带首尾引号（中英文皆有）：去引号后再校验，否则绝对路径也会被误报
        cleaned = [p.strip().strip("\"'“”‘’") for p in paths]
        if any(not p for p in cleaned):
            raise ValueError("存在空路径")
        if any(re.search(r"[\s\x00-\x1f]", p) for p in cleaned):
            raise ValueError("路径含空白字符（systemd 会按空白拆散），请改名")
        abs_paths = [str(Path(p).expanduser()) for p in cleaned]
        if any(not p.startswith("/") for p in abs_paths):
            raise ValueError("只接受绝对路径")
        for p in abs_paths:
            pp = Path(p)
            if not pp.exists():
                raise ValueError(f"路径不存在：{p}")
            if pp.is_dir():
                if not list(pp.glob("*.mp4")):
                    raise ValueError(f"目录无 mp4：{p}")
            elif not (pp.is_file() and pp.suffix.lower() == ".mp4"):
                raise ValueError(f"不是 mp4 文件：{p}")
        encode = bool(data.get("encode", False))
        ensure_room_live()  # 关播时用上次分区/标题自动开播，否则切了也推不上去
        args = " ".join(abs_paths) + (" --encode" if encode else "")
        _write_env(REPLAY_ENV, [f"REPLAY_ARGS={args}"])
        _ctl("disable", "--now", LIVE_UNIT)
        if not _wait_inactive(LIVE_UNIT):
            raise RuntimeError("直播推流尚未停止，已取消启动文件轮播")
        # 意图性重启不受 crash 熔断计数限制：先清 start-limit，否则连续切换直接 400 且单元变 failed
        run([*SYSTEMCTL, "reset-failed", REPLAY_UNIT], check=False)
        _ctl("enable", REPLAY_UNIT)
        _ctl("restart", REPLAY_UNIT)  # 新 REPLAY_ARGS 只在新进程生效
    else:
        raise ValueError('mode 只能是 "live" 或 "replay"')
    return status()


@exclusive
def stop_all() -> dict[str, object]:
    _ctl("disable", "--now", LIVE_UNIT)
    _ctl("disable", "--now", REPLAY_UNIT)
    return status()


@exclusive
def room(data: dict) -> dict[str, object]:
    action = str(data.get("action", ""))
    if action == "start":
        try:
            area = int(data.get("area", 0))
        except (TypeError, ValueError):
            raise ValueError("area 须为数字（live.py areas 查子分区 ID）")
        if area <= 0:
            raise ValueError("area 须为正整数")
        title = str(data.get("title", "")).strip()
        if not title or len(title) > 40 or "\n" in title:
            raise ValueError("title 非空、≤40 字符、禁 emoji/换行")
        r = run(["python3", str(LIVE_PY), "start", "--area", str(area), "--title", title],
                timeout=90, check=False)
        if r.returncode != 0:
            raise RuntimeError((r.stdout + r.stderr).strip()[-2000:] or "开播失败")
        sync_push_env()
        # 推流码已换：运行中的推流进程拿的还是旧码，必须重启才生效
        for unit in (LIVE_UNIT, REPLAY_UNIT):
            if _show(unit)["active"] == "active":
                _ctl("restart", unit)
        return {"ok": True, "output": r.stdout.strip()[-2000:], "status": status()}
    if action == "stop":
        r = run(["python3", str(LIVE_PY), "stop"], timeout=60, check=False)
        if r.returncode != 0:
            raise RuntimeError((r.stdout + r.stderr).strip()[-2000:] or "停播失败")
        _ctl("disable", "--now", LIVE_UNIT)
        _ctl("disable", "--now", REPLAY_UNIT)
        return {"ok": True, "output": r.stdout.strip()[-1000:], "status": status()}
    if action == "update":
        title = str(data.get("title", "")).strip()
        if not title or len(title) > 40 or "\n" in title:
            raise ValueError("title 非空、≤40 字符、禁 emoji/换行")
        r = run(["python3", str(LIVE_PY), "update", "--title", title], timeout=60, check=False)
        if r.returncode != 0:
            raise RuntimeError((r.stdout + r.stderr).strip()[-2000:] or "改标题失败")
        return {"ok": True, "output": r.stdout.strip()[-1000:]}
    raise ValueError('action 只能是 "start"、"stop" 或 "update"')


def unit_logs(which: str, tail: int) -> str:
    if which == "live":
        unit = LIVE_UNIT
    elif which == "replay":
        unit = REPLAY_UNIT
    elif which == "webui":
        unit = WEBUI_UNIT
    else:
        raise ValueError("which 只能是 live、replay 或 webui")
    tail = max(1, min(int(tail), 1000))
    r = run(["journalctl", "--user", "-u", unit, "-n", str(tail), "--no-pager", "-o", "short-iso"],
            check=False)
    return r.stdout[-100_000:]


class Handler(BaseHTTPRequestHandler):
    server_version = "BiliPushWebUI/1.0"

    def send_json(self, code: int, payload: dict | list) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def authenticated(self) -> bool:
        # 认证已移除：仅监听回环地址；公网发布必须经反代加 Basic Auth。
        return True

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        try:
            if parsed.path in ("/", "/index.html"):
                body = INDEX_FILE.read_bytes()
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(body)
            elif parsed.path == "/api/health":
                self.send_json(HTTPStatus.OK, {"ok": True})
            elif parsed.path == "/api/status":
                self.send_json(HTTPStatus.OK, status())
            elif parsed.path == "/api/logs":
                q = parse_qs(parsed.query)
                self.send_json(HTTPStatus.OK, {
                    "logs": unit_logs(q.get("which", ["live"])[0],
                                      int(q.get("tail", ["200"])[0] or "200")),
                })
            else:
                self.send_json(HTTPStatus.NOT_FOUND, {"error": "not found"})
        except (ValueError, RuntimeError, OSError, subprocess.TimeoutExpired) as exc:
            self.send_json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})

    def do_POST(self) -> None:  # noqa: N802
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length < 0 or length > 16_384:
                raise ValueError("请求过大")
            data = json.loads(self.rfile.read(length) or b"{}")
            if not isinstance(data, dict):
                raise ValueError("请求内容须为 JSON 对象")
            if self.path == "/api/mode":
                self.send_json(HTTPStatus.OK, set_mode(data))
            elif self.path == "/api/stop":
                self.send_json(HTTPStatus.OK, stop_all())
            elif self.path == "/api/room":
                self.send_json(HTTPStatus.OK, room(data))
            else:
                self.send_json(HTTPStatus.NOT_FOUND, {"error": "not found"})
        except ControlBusy as exc:
            self.send_json(HTTPStatus.CONFLICT, {"error": str(exc)})
        except (ValueError, RuntimeError, json.JSONDecodeError,
                OSError, subprocess.TimeoutExpired) as exc:
            self.send_json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})

    def log_message(self, fmt: str, *args: object) -> None:
        print(f"{self.address_string()} - {fmt % args}")


def main() -> None:
    host = os.environ.get("BILI_WEBUI_HOST", "127.0.0.1")
    port = int(os.environ.get("BILI_WEBUI_PORT", "8767"))
    server = ThreadingHTTPServer((host, port), Handler)
    print(f"Bili Push WebUI: http://{host}:{port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
