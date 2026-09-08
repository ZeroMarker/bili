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
import re
import sys
import time

# 各平台 FLV 拉流清晰度 key → 近似视频高度（ORIGIN 为源流，按 1080 处理）。
FLV_QUALITY_KEYS: tuple[tuple[str, int | None], ...] = (
    ("ORIGIN", 1080),
    ("FULL_HD1", 1080),
    ("HD1", 720),
    ("SD1", 480),
    ("SD2", 360),
)


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
                if isinstance(url, str) and url.startswith("http"):
                    return url
        elif isinstance(stream_url, str) and stream_url.startswith("http"):
            return stream_url
        # 直接挂在 data 上的 rtmp/hls
        for key in ("rtmp_pull_url", "hls_pull_url", "liveUrl"):
            url = room_info.get(key)
            if isinstance(url, str) and url.startswith("http"):
                return url
    return None


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

    print("[tiktok_fallback] 所有 API 检测均未发现直播", file=sys.stderr)
    return None
