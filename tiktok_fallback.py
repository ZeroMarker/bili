#!/usr/bin/env python3
"""TikTok webcast 兜底取流（vendor 自 tiktok 项目，零外部文件依赖）。

来源：~/tiktok/scripts/dlr/adapters/tiktok_extract.py（+ base.py 的清晰度表），
逻辑逐行对齐：curl_cffi 直解 /live 页面 → SIGI_STATE / Universal Data /
全文 roomId → webcast API 拿流（FLV 优先，rtmp/HLS 兜底）。
与上游的唯一差异：去掉了其内部的 yt-dlp 重试步骤——调用方 get_stream.py
已经跑过 4 种 yt-dlp 变体（含 Cookie），是其超集。

依赖：curl_cffi（pip 包，非文件；缺失时 get_stream_url 返回 None）。
标准库 only，其余无依赖。
"""

from __future__ import annotations

import json
import os
import re
import signal
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

# 各平台 FLV 拉流清晰度 key → 近似视频高度（ORIGIN 为源流，按 1080 处理）。
FLV_QUALITY_KEYS: tuple[tuple[str, int | None], ...] = (
    ("ORIGIN", 1080),
    ("FULL_HD1", 1080),
    ("HD1", 720),
    ("SD1", 480),
    ("SD2", 360),
)

STREAM_SCHEMES = ("http://", "https://", "rtmp://", "rtmps://")


def is_stream_url(value: object) -> bool:
    """Return whether *value* is a pull URL supported by ffmpeg."""
    return isinstance(value, str) and value.lower().startswith(STREAM_SCHEMES)


def pick_flv_url(flv: object, max_height: int | None = None) -> str | None:
    """从 FLV 拉流字典中按目标高度挑选 URL。

    max_height 为 None 时取最高可用清晰度（原画档）；否则返回不超过上限的最高
    可用清晰度；若所有可用清晰度都超过上限，则退回最低可用清晰度，保证可录。
    """
    if not isinstance(flv, dict):
        return None
    candidates: list[tuple[int | None, str]] = []
    for key, height in FLV_QUALITY_KEYS:
        url = flv.get(key)
        if isinstance(url, str) and url:
            candidates.append((height, url))
    if not candidates:
        return None
    for height, url in candidates:
        if max_height is None or height is None or height <= max_height:
            return url
    return candidates[-1][1]


def _request_with_retry(
    session,
    url: str,
    *,
    params: dict | None = None,
    impersonate: str = "chrome131",
    timeout: int = 20,
    attempts: int = 3,
    base_delay: float = 1.0,
    max_delay: float = 8.0,
) -> object | None:
    """GET 请求容错封装：网络异常（TLS/超时/连接重置）按指数退避重试。

    返回 Response；重试耗尽仍失败时返回 None，不抛异常（调用方继续判断/循环）。
    """
    from curl_cffi import requests as curl_requests

    for attempt in range(attempts):
        try:
            return session.get(
                url,
                params=params,
                impersonate=impersonate,
                timeout=timeout,
            )
        except (curl_requests.exceptions.RequestException, OSError) as exc:
            if attempt + 1 < attempts:
                delay = min(base_delay * (2 ** attempt), max_delay)
                print(
                    f"[tiktok_fallback] 请求失败({type(exc).__name__}) {url}，"
                    f"{delay:g}s 后重试（{attempt + 1}/{attempts}）",
                    file=sys.stderr,
                )
                time.sleep(delay)
    return None


def get_room_id_from_sigi(text: str) -> tuple[str | None, int]:
    """从 SIGI_STATE 中提取 liveRoom 信息。Returns: (room_id, status_code)。"""
    match = re.search(
        r'<script id="SIGI_STATE"[^>]*>(.*?)</script>', text, re.DOTALL
    )
    if not match:
        return None, -1

    sigi = json.loads(match.group(1))
    lr = sigi.get("LiveRoom", {})

    # 检查 liveRoomUserInfo.liveRoom.status
    room_info = lr.get("liveRoomUserInfo", {}).get("liveRoom", {})
    status = room_info.get("status", 0)
    room_id = room_info.get("roomId")

    # 如果 status != 2 或 room_id 为空，也检查 CurrentRoom
    if status != 2 or not room_id:
        cr = sigi.get("CurrentRoom", {})
        if cr:
            cr_id = cr.get("roomId")
            if cr_id:
                room_id = str(cr_id)
                status = 2  # CurrentRoom 有值视为直播中

    return room_id, status


def get_room_id_from_universal(text: str) -> str | None:
    """从 __UNIVERSAL_DATA_FOR_REHYDRATION__ 中提取 roomId。

    这个值可能是用户永久 roomId，不一定是当前直播的 roomId。
    """
    match = re.search(
        r'<script[^>]*id="__UNIVERSAL_DATA_FOR_REHYDRATION__"[^>]*>'
        r"(.*?)</script>",
        text,
        re.DOTALL,
    )
    if not match:
        return None
    data = json.loads(match.group(1))
    default_scope = data.get("__DEFAULT_SCOPE__", {})

    # 优先从 webcast-sse.user-detail 或 webcast.user-detail 中取
    for key in ("webcast.user-detail", "webcast-sse.user-detail", "webapp.user-detail"):
        ud = default_scope.get(key, {})
        if ud:
            room_id = ud.get("userInfo", {}).get("user", {}).get("roomId", "")
            if room_id:
                return str(room_id)
    return None


def check_live_via_webcast_api(
    session, room_id: str, max_height: int | None = None
) -> str | None:
    """直接调用 webcast API 检查直播状态，返回流 URL（如果有）。

    max_height 为 None 时返回最高可用清晰度（原画档），否则返回不超过上限的清晰度。
    """
    params = {"room_id": room_id, "aid": "1988"}
    r = _request_with_retry(
        session,
        "https://webcast.tiktok.com/webcast/room/info/",
        params=params,
        impersonate="chrome131",
        timeout=15,
    )
    if r is None:
        return None

    try:
        data = r.json()
    except Exception:
        return None

    status_code = data.get("status_code")
    if status_code != 0:
        return None

    room_info = data.get("data", {})
    if room_info.get("status") == 2:
        # 提取流 URL：stream_url 可能是一个包含各清晰度/协议的字典
        stream_url = room_info.get("stream_url") or {}
        if isinstance(stream_url, dict):
            # 优先 FLV 按清晰度挑选，其次 rtmp/hls
            flv = stream_url.get("flv_pull_url") or {}
            url = pick_flv_url(flv, max_height)
            if url:
                return url
            for key in ("rtmp_pull_url", "hls_pull_url", "liveUrl"):
                url = stream_url.get(key)
                if is_stream_url(url):
                    return url
        elif is_stream_url(stream_url):
            return stream_url
        # 直接挂在 data 上的 rtmp/hls
        for key in ("rtmp_pull_url", "hls_pull_url", "liveUrl"):
            url = room_info.get(key)
            if is_stream_url(url):
                return url
    return None


def _stream_url_from_sigi(text: str) -> str | None:
    """Extract a playable FLV/HLS URL embedded in the rendered SIGI_STATE."""
    match = re.search(r'<script id="SIGI_STATE"[^>]*>(.*?)</script>', text, re.DOTALL)
    if not match:
        return None
    try:
        data = json.loads(match.group(1))
    except (TypeError, ValueError):
        return None

    urls: list[str] = []
    def walk(value: object, key: str = "") -> None:
        if isinstance(value, dict):
            for child_key, child in value.items():
                walk(child, str(child_key))
        elif isinstance(value, list):
            for child in value:
                walk(child, key)
        elif isinstance(value, str) and is_stream_url(value):
            # Skip the audio-only variant when the page also exposes video.
            if "only_audio=1" not in value:
                urls.append(value)
        elif isinstance(value, str) and key in {"stream_data", "streamData"}:
            # Recent TikTok pages serialize pull_data.stream_data as JSON.
            try:
                walk(json.loads(value), key)
            except (TypeError, ValueError):
                pass

    live_room = data.get("LiveRoom", {}).get("liveRoomUserInfo", {}).get("liveRoom", {})
    walk(live_room.get("streamData", {}))
    walk(live_room.get("hevcStreamData", {}))
    # FLV is more stable for long-running relay than HLS.
    return next((url for url in urls if ".flv" in url), None) or (urls[0] if urls else None)


def _wait_for_process_group_exit(pgid: int, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while True:
        try:
            os.killpg(pgid, 0)
        except ProcessLookupError:
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.05)


def _terminate_process_group(proc: subprocess.Popen, grace: float = 5) -> None:
    """Terminate and reap Chromium and every process in its session."""
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    try:
        proc.communicate(timeout=grace)
    except subprocess.TimeoutExpired:
        pass
    if _wait_for_process_group_exit(proc.pid, grace):
        return
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    try:
        proc.communicate(timeout=grace)
    except subprocess.TimeoutExpired:
        pass
    if not _wait_for_process_group_exit(proc.pid, grace):
        raise subprocess.TimeoutExpired(proc.args, grace)


def _active_chromium_profiles() -> set[str]:
    profiles: set[str] = set()
    for cmdline in Path("/proc").glob("[0-9]*/cmdline"):
        try:
            args = cmdline.read_bytes().split(b"\0")
        except OSError:
            continue
        for arg in args:
            if arg.startswith(b"--user-data-dir="):
                profiles.add(os.fsdecode(arg.partition(b"=")[2]))
    return profiles


def _remove_stale_chromium_profiles(
    profile_parent: Path, *, max_age: float = 600
) -> None:
    """Remove abandoned Bili probe profiles, preserving live/recent probes."""
    active = _active_chromium_profiles()
    cutoff = time.time() - max_age
    for profile in profile_parent.glob("bili-tiktok-chromium-*"):
        try:
            if profile.stat().st_mtime > cutoff or str(profile) in active:
                continue
        except OSError:
            continue
        shutil.rmtree(profile, ignore_errors=True)


def _chromium_profile_parent(browser: str) -> str | None:
    browser_path = Path(browser)
    if browser_path.parts[:3] != ("/", "snap", "bin"):
        return None
    profile_parent = (
        Path.home() / "snap" / browser_path.name / "common" / "chromium-headless"
    )
    profile_parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    _remove_stale_chromium_profiles(profile_parent)
    return str(profile_parent)


def _install_termination_handlers(
    proc: subprocess.Popen,
) -> dict[signal.Signals, object]:
    """Forward service stop signals to Chromium before terminating Python."""
    previous: dict[signal.Signals, object] = {}

    def handle(signum: int, _frame) -> None:
        # Do not call communicate() recursively from a signal handler while
        # the outer communicate() is active. Stop the disposable browser
        # immediately; the surrounding finally block reaps it and cleans the
        # profile after this SystemExit unwinds the interrupted wait.
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        raise SystemExit(128 + signum)

    try:
        for signum in (signal.SIGTERM, signal.SIGINT, signal.SIGQUIT):
            previous[signum] = signal.getsignal(signum)
            signal.signal(signum, handle)
    except ValueError:
        # signal.signal() is only available on the main thread. Current CLI
        # callers use the main thread; library callers still retain finally
        # cleanup and the stale-profile safety net.
        return {}
    return previous


def _restore_signal_handlers(previous: dict[signal.Signals, object]) -> None:
    for signum, handler in previous.items():
        signal.signal(signum, handler)


def _get_stream_url_with_browser(username: str, timeout: int = 35) -> str | None:
    """Resolve SlardarWAF pages using the installed headless Chromium, if any."""
    browser = next(
        (shutil.which(name) for name in ("chromium", "chromium-browser", "google-chrome")
         if shutil.which(name)),
        None,
    )
    if not browser:
        return None
    url = f"https://www.tiktok.com/@{username}/live"
    try:
        profile_parent = _chromium_profile_parent(browser)
        with tempfile.TemporaryDirectory(
            prefix="bili-tiktok-chromium-", dir=profile_parent
        ) as profile:
            proc = subprocess.Popen(
                [browser, "--headless=new", "--no-sandbox", "--disable-gpu",
                 "--disable-dev-shm-usage", f"--user-data-dir={profile}",
                 "--virtual-time-budget=15000", "--dump-dom", url],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                start_new_session=True,
            )
            previous_handlers = _install_termination_handlers(proc)
            try:
                stdout, _ = proc.communicate(timeout=timeout)
            finally:
                _restore_signal_handlers(previous_handlers)
                _terminate_process_group(proc)
    except (OSError, subprocess.TimeoutExpired) as exc:
        print(f"[tiktok_fallback] 浏览器兜底失败：{exc}", file=sys.stderr)
        return None
    stream_url = _stream_url_from_sigi(stdout or "")
    if stream_url:
        print("[tiktok_fallback] 浏览器渲染通过 WAF，已从页面取得 FLV", file=sys.stderr)
    return stream_url


def get_stream_url(username: str) -> str | None:
    """兜底取流主入口：成功返回一行流 URL，失败返回 None（原画档）。"""
    try:
        from curl_cffi import requests
    except ImportError:
        print("缺少 curl_cffi：pip install curl_cffi", file=sys.stderr)
        return None

    live_url = f"https://www.tiktok.com/@{username}/live"

    session = requests.Session()
    _request_with_retry(session, "https://www.tiktok.com", attempts=1)

    print(f"[tiktok_fallback] 检查 @{username} ...", file=sys.stderr)

    r = _request_with_retry(session, live_url, impersonate="chrome131", timeout=20)
    if r is None:
        print("[tiktok_fallback] 多次重试仍无法访问直播页，本次放弃", file=sys.stderr)
        return None

    # SlardarWAF returns a small "Please wait..." page to curl_cffi. A real
    # browser can complete that challenge and receives the rendered SIGI_STATE.
    if "slardar" in r.text.lower() or "please wait" in r.text.lower():
        stream_url = _get_stream_url_with_browser(username)
        if stream_url:
            return stream_url

    # 方法A：SIGI_STATE 检测
    room_id, status = get_room_id_from_sigi(r.text)
    if status == 2 and room_id:
        print(f"[tiktok_fallback] SIGI_STATE status=2, roomId={room_id}", file=sys.stderr)
        stream_url = check_live_via_webcast_api(session, room_id)
        if stream_url:
            return stream_url

    # 方法B：Universal Data 检测
    ud_room_id = get_room_id_from_universal(r.text)
    if ud_room_id:
        print(
            f"[tiktok_fallback] universal data roomId={ud_room_id}",
            file=sys.stderr,
        )
        stream_url = check_live_via_webcast_api(session, ud_room_id)
        if stream_url:
            return stream_url

    # 方法C：在页面中搜索 roomId 再试
    all_room_ids = re.findall(r'"roomId":"(\d+)"', r.text)
    for rid in set(all_room_ids):
        if rid and rid != "0":
            print(f"[tiktok_fallback] 尝试 roomId={rid} ...", file=sys.stderr)
            stream_url = check_live_via_webcast_api(session, rid)
            if stream_url:
                return stream_url

    # The browser-rendered page may contain streamData even when room/info is
    # unavailable for this account/network combination.
    stream_url = _get_stream_url_with_browser(username)
    if stream_url:
        return stream_url

    print("[tiktok_fallback] 所有 API 检测均未发现直播", file=sys.stderr)
    return None
