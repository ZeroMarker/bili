"""live.py — Bilibili 直播开播工具（移植自 obs-bilibili-stream 插件）。

来源：
    上游仓库：https://github.com/Zarosmm/obs-bilibili-stream（GNU GPL v2.0）
    对应上游文件：``src/bilibili_api.cpp`` / ``src/bilibili_api.hpp``
    （``appsign``、``getQrCode``、``qrLogin``、``checkLoginStatus``、
    ``getRoomIdAndCsrf``、``getPartitionList``、``startLive``、``stopLive``、
    ``updateRoomInfo``；``APP_KEY``/``APP_SECRET`` 取自上游 ``bilibili_api.hpp``；
    Cookie 拼接与 crossDomain ticket 兜底对应上游 ``src/http_client.cpp`` 的
    Set-Cookie 收集逻辑；终端二维码对应上游 ``src/core/qr_generator.cpp`` +
    ``src/qrcodegen/``（ECC LOW），编码器为同族 Nayuki Python 实现，
    vendored 于 ``qrcodegen.py``（MIT，头部保留原作者声明）
    本文件作为上游 GPL-2.0 代码的衍生移植，受上游 GPL-2.0 条款约束，
    与本仓库其余 MIT 代码不同（见仓库根 ``LICENSE``）。

覆盖插件 ``src/bilibili_api.cpp`` 的全部流程，方便转推脚本自动获取
RTMP 地址与推流码，替代手填 BILIBILI_PUSH_URL / BILIBILI_PUSH_CODE：

    扫码登录 → 登录态检查 → 解析 room_id/csrf → 开播取 RTMP → 停播 / 改标题

用法：
    python3 live.py login [--session FILE]
    python3 live.py status
    python3 live.py areas
    python3 live.py start --area 86 [--title TITLE] [--print-export]
    python3 live.py stop
    python3 live.py update --title TITLE

会话默认存为 ``.bilibili_session.json``（权限 600，已 gitignore），
只含 Cookie / room_id / csrf / mid / rtmp 信息。Cookie 与推流码不会写入日志。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
import urllib.parse
import urllib.request
from http.client import HTTPResponse
from pathlib import Path
APP_KEY = "aae92bc66f3edfab"  # 来源：上游 src/bilibili_api.hpp（Bilibili 开放平台密钥，非本项目生成）
APP_SECRET = "af125a0d5279fd576c1b4418a3e8276d"  # 同上

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36"
)
# 与上游 ``src/bilibili_api.cpp`` 的 default_headers 一致：
# 直播接口会校验 Origin/Referer，缺失会导致签名错误（-3）。
DEFAULT_HEADERS = {
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8,en-GB;q=0.7,en-US;q=0.6",
    "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
    "Origin": "https://link.bilibili.com",
    "Referer": "https://link.bilibili.com/p/center/index",
    "Sec-Ch-Ua": '"Microsoft Edge";v="129", "Not=A?Brand";v="8", "Chromium";v="129"',
    "Sec-Ch-Ua-Mobile": "?0",
    "Sec-Ch-Ua-Platform": '"Windows"',
    "Sec-Fetch-Dest": "empty",
    "Sec-Fetch-Mode": "cors",
    "Sec-Fetch-Site": "same-site",
    "User-Agent": UA,
}
TIMEOUT = 15
DEFAULT_SESSION = Path(__file__).resolve().parent / ".bilibili_session.json"

QR_GENERATE_URL = "https://passport.bilibili.com/x/passport-login/web/qrcode/generate"
QR_POLL_URL = "https://passport.bilibili.com/x/passport-login/web/qrcode/poll"
NAV_URL = "https://api.bilibili.com/x/web-interface/nav"
ROOM_ID_URL = "https://api.live.bilibili.com/room/v2/Room/room_id_by_uid"
AREA_LIST_URL = "https://api.live.bilibili.com/room/v1/Area/getList"
VERSION_URL = "https://api.live.bilibili.com/xlive/app-blink/v1/liveVersionInfo/getHomePageLiveVersion"
START_LIVE_URL = "https://api.live.bilibili.com/room/v1/Room/startLive"
STOP_LIVE_URL = "https://api.live.bilibili.com/room/v1/Room/stopLive"
UPDATE_ROOM_URL = "https://api.live.bilibili.com/room/v1/Room/update"


class BiliError(RuntimeError):
    """可直接展示给用户的 B 站接口错误。"""


def appsign(params: list[tuple[str, str]], app_key: str, app_secret: str) -> str:
    """WBI appsign：排序 + appkey → query + secret 取 MD5 → 追加 sign。

    与插件 ``BiliApi::appsign`` 逐行对应。
    """
    items = sorted(params) + [("appkey", app_key)]
    query = "&".join(f"{k}={v}" for k, v in items)
    digest = hashlib.md5((query + app_secret).encode("utf-8")).hexdigest()
    items.append(("sign", digest))
    return "&".join(f"{k}={v}" for k, v in items)


def parse_set_cookies(msg) -> str:
    """从 HTTP 头收集所有 Set-Cookie 的 ``k=v`` 部分并用 ``; `` 拼接。"""
    get_all = getattr(msg, "get_all_matching_headers", None)
    if get_all is None:  # pragma: no cover - 防御性分支
        raw = str(msg)
        lines = raw.splitlines()
    else:
        lines = get_all("Set-cookie")
    parts: list[str] = []
    for line in lines:
        line = line.strip()
        if ":" in line:
            line = line.split(":", 1)[1].strip()
        token = line.split(";", 1)[0].strip()
        if token and "=" in token:
            parts.append(token)
    return "; ".join(parts)


def extract_cookie_value(cookies: str, key: str) -> str:
    """从 ``a=1; b=2`` 风格的 Cookie 串提取单个值，缺失返回空串。"""
    for part in cookies.split(";"):
        part = part.strip()
        if part.startswith(key + "="):
            return part[len(key) + 1 :]
    return ""


def extract_url_param(url: str, key: str) -> str:
    """从 URL 查询串提取参数（兼容插件旧登录流的 URL 解析兜底）。"""
    qs = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
    vals = qs.get(key)
    return vals[0] if vals else ""


def render_qr_terminal(text: str, border: int = 1) -> str:
    """把文本渲染为终端二维码（``██``/双空格块，每模块两列以保正方形）。

    编码参数与上游 ``src/core/qr_generator.cpp`` 一致（ECC LOW）；
    编码器为同族 Nayuki 实现（见 ``qrcodegen.py`` 头部来源）。
    """
    from qrcodegen import QrCode

    qr = QrCode.encode_text(text, QrCode.Ecc.LOW)
    size = qr.get_size()
    lines = []
    for y in range(-border, size + border):
        lines.append("".join("██" if qr.get_module(x, y) else "  " for x in range(-border, size + border)))
    return "\n".join(lines)


def _request(
    url: str,
    *,
    cookies: str = "",
    data: str | None = None,
    timeout: int = TIMEOUT,
) -> tuple[dict, str]:
    """发请求并返回 ``(json_body, set_cookie_header)``。非 200 或 code != 0 由调用方判定。"""
    headers = dict(DEFAULT_HEADERS)
    if cookies:
        headers["Cookie"] = cookies
    body = data.encode("utf-8") if data is not None else None
    req = urllib.request.Request(url, data=body, headers=headers, method="POST" if body else "GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
            assert isinstance(resp, HTTPResponse)
            raw = resp.read().decode("utf-8", "replace")
            set_cookies = parse_set_cookies(resp.headers)
    except OSError as exc:
        raise BiliError(f"网络错误：{exc}") from exc
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise BiliError(f"JSON 解析失败：{exc}") from exc
    if not isinstance(payload, dict):
        raise BiliError("接口返回异常（非 JSON 对象）")
    return payload, set_cookies


def qr_generate(cookies: str = "") -> tuple[str, str]:
    payload, _ = _request(QR_GENERATE_URL, cookies=cookies)
    data = payload.get("data") or {}
    url, key = data.get("url", ""), data.get("qrcode_key", "")
    if not url or not key:
        raise BiliError(f"获取二维码失败：{payload.get('message', payload)}")
    return url, key


def qr_poll(qr_key: str) -> tuple[dict, str]:
    """轮询一次扫码状态，返回 ``(data, set_cookies)``。"""
    url = f"{QR_POLL_URL}?qrcode_key={urllib.parse.quote(qr_key)}"
    payload, set_cookies = _request(url)
    return payload.get("data") or {}, set_cookies


def check_login(cookies: str) -> tuple[bool, str]:
    payload, _ = _request(NAV_URL, cookies=cookies)
    data = payload.get("data") or {}
    if data.get("isLogin"):
        return True, str(data.get("mid", ""))
    return False, ""


def resolve_room(cookies: str) -> tuple[str, str]:
    """由 Cookie 推导 room_id 与 csrf（bili_jct），对应插件 getRoomIdAndCsrf。"""
    uid = extract_cookie_value(cookies, "DedeUserID")
    csrf = extract_cookie_value(cookies, "bili_jct")
    if not uid:
        raise BiliError("Cookie 缺少 DedeUserID，请重新扫码登录")
    if not csrf:
        raise BiliError("Cookie 缺少 bili_jct，请重新扫码登录")
    payload, _ = _request(f"{ROOM_ID_URL}?uid={urllib.parse.quote(uid)}", cookies=cookies)
    if payload.get("code") != 0:
        raise BiliError(f"获取房间号失败：{payload.get('message')}")
    room_id = str((payload.get("data") or {}).get("room_id", ""))
    if not room_id or room_id == "0":
        raise BiliError("获取房间号失败：返回为空")
    return room_id, csrf


def get_areas() -> list:
    payload, _ = _request(AREA_LIST_URL)
    data = payload.get("data")
    if not isinstance(data, list):
        raise BiliError(f"获取分区列表失败：{payload.get('message', '无数据数组')}")
    return data


def start_live(cookies: str, room_id: str, csrf: str, area_id: int) -> tuple[str, str]:
    version_qs = appsign([("system_version", "2"), ("ts", str(int(time.time())))], APP_KEY, APP_SECRET)
    payload, _ = _request(f"{VERSION_URL}?{version_qs}", cookies=cookies)
    if payload.get("code") != 0:
        raise BiliError(f"获取直播版本失败：{payload.get('message')}")
    data = payload.get("data") or {}
    version = data.get("curr_version")
    if not version:
        raise BiliError("获取直播版本失败：curr_version 为空")
    # 偏离上游：上游还签名 ``build``（取自 blink 版本接口），实测含 build
    # 必回 -3 签名错误（2026-09-06，build=11025），去 build 后签名通过；
    # ``version`` 保留（签名安全），其余参数与上游一致。
    form = appsign(
        [
            ("room_id", room_id),
            ("platform", "pc_link"),
            ("area_v2", str(area_id)),
            ("backup_stream", "0"),
            ("csrf_token", csrf),
            ("csrf", csrf),
            ("version", str(version)),
            ("ts", str(int(time.time()))),
        ],
        APP_KEY,
        APP_SECRET,
    )
    payload, _ = _request(START_LIVE_URL, cookies=cookies, data=form)
    code = payload.get("code", -1)
    if code != 0:
        data = payload.get("data") or {}
        face_url = data.get("qr", "")
        if code in (60024, 60043):  # 需人脸验证，与插件一致
            if code == 60043:
                raise BiliError("需要人脸验证：https://www.bilibili.com/blackboard/live/face-auth-middle.html")
            raise BiliError(f"需要人脸验证，请扫码：{face_url}")
        raise BiliError(f"开播失败 [{code}]：{payload.get('message')}")
    rtmp = (payload.get("data") or {}).get("rtmp") or {}
    addr, stream_code = rtmp.get("addr", ""), rtmp.get("code", "")
    if not addr or not stream_code:
        raise BiliError("开播成功但未返回 RTMP 地址/推流码")
    return addr, stream_code


def stop_live(cookies: str, room_id: str, csrf: str) -> None:
    form = f"room_id={urllib.parse.quote(room_id)}&platform=pc_link&csrf_token={csrf}&csrf={csrf}"
    payload, _ = _request(STOP_LIVE_URL, cookies=cookies, data=form)
    if payload.get("code") != 0:
        raise BiliError(f"停播失败：{payload.get('message')}")


def update_room(cookies: str, room_id: str, csrf: str, title: str) -> None:
    form = (
        f"room_id={urllib.parse.quote(room_id)}&platform=pc_link"
        f"&title={urllib.parse.quote(title)}&csrf_token={csrf}&csrf={csrf}"
    )
    payload, _ = _request(UPDATE_ROOM_URL, cookies=cookies, data=form)
    if payload.get("code") != 0:
        raise BiliError(f"更新直播间信息失败：{payload.get('message')}")


def load_session(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise BiliError(f"会话文件损坏：{path}（{exc}），可删除后重新 login") from exc


def save_session(path: Path, session: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(session, ensure_ascii=False, indent=2), encoding="utf-8")
    os.chmod(tmp, 0o600)
    tmp.replace(path)


def cmd_login(args: argparse.Namespace) -> int:
    session_path: Path = args.session
    old = load_session(session_path)
    url, qr_key = qr_generate(old.get("cookies", ""))
    print("请用 Bilibili App 扫码登录：")
    print(render_qr_terminal(url))
    print(f"扫不上时手动复制 URL 到手机浏览器/扫码工具：\n{url}")
    print("（二维码 3 分钟内有效；已扫码待确认时会提示）")
    deadline = time.time() + args.timeout
    while time.time() < deadline:
        data, set_cookies = qr_poll(qr_key)
        code = data.get("code", -1)
        if code == 0:
            cookies = set_cookies
            if not cookies and data.get("url"):  # 2026-08 新登录流：跟随 ticket 拿 Cookie
                try:
                    _, cookies = _request(data["url"])
                except BiliError:
                    cookies = ""
            if not cookies:  # 旧登录流兜底：从跳转 URL 解析
                sess = extract_url_param(data.get("url", ""), "SESSDATA")
                jct = extract_url_param(data.get("url", ""), "bili_jct")
                uid = extract_url_param(data.get("url", ""), "DedeUserID")
                if sess and jct:
                    cookies = f"SESSDATA={sess}; bili_jct={jct}; DedeUserID={uid};"
            if not cookies:
                raise BiliError("登录成功但未获取到 Cookie")
            ok, mid = check_login(cookies)
            if not ok:
                raise BiliError("Cookie 校验未通过")
            room_id, csrf = resolve_room(cookies)
            save_session(
                session_path,
                {**old, "cookies": cookies, "mid": mid, "room_id": room_id, "csrf_token": csrf},
            )
            print(f"登录成功 mid={mid} room_id={room_id}")
            return 0
        if code == 86090:
            print("已扫码，等待手机确认…")
        elif code == 86038:
            raise BiliError("二维码已失效，请重新执行 login")
        elif code == 86101:
            pass  # 未扫码，静默继续轮询
        else:
            raise BiliError(f"扫码异常 [{code}]：{data.get('message', '')}")
        time.sleep(args.poll_interval)
    raise BiliError("扫码超时，请重新执行 login")


def cmd_status(args: argparse.Namespace) -> int:
    session = load_session(args.session)
    if not session.get("cookies"):
        print("未登录：请先执行 `python3 live.py login`")
        return 1
    ok, mid = check_login(session["cookies"])
    print(f"登录状态：{'已登录' if ok else '未登录'}", end="")
    if ok:
        print(f" mid={mid} room_id={session.get('room_id', '')}", end="")
    print()
    return 0 if ok else 1


def cmd_areas(_args: argparse.Namespace) -> int:
    for group in get_areas():
        print(f"[{group.get('id')}] {group.get('name')}")
        for sub in group.get("list") or []:
            print(f"  {sub.get('id')}: {sub.get('name')}")
    return 0


def _require_authed(args: argparse.Namespace) -> tuple[dict, str, str]:
    session = load_session(args.session)
    cookies = session.get("cookies", "")
    if not cookies:
        raise BiliError("未登录：请先执行 `python3 live.py login`")
    room_id = session.get("room_id") or ""
    csrf = session.get("csrf_token") or ""
    if not room_id or not csrf:  # 兼容老会话：缺字段时重新推导
        room_id, csrf = resolve_room(cookies)
        session.update({"room_id": room_id, "csrf_token": csrf})
        save_session(args.session, session)
    return session, room_id, csrf


def cmd_start(args: argparse.Namespace) -> int:
    session, room_id, csrf = _require_authed(args)
    addr, stream_code = start_live(session["cookies"], room_id, csrf, args.area)
    session.update(
        {
            "rtmp_addr": addr,
            "rtmp_code": stream_code,
            "area_id": args.area,
            "title": args.title or session.get("title", ""),
        }
    )
    if args.title:
        try:
            update_room(session["cookies"], room_id, csrf, args.title)
        except BiliError as exc:
            print(f"开播成功，但更新标题失败：{exc}")
    save_session(args.session, session)
    print("开播成功")
    print(f"RTMP 地址：{addr}")
    print("推流码已保存到会话文件（不在终端回显全码）")
    if args.print_export:
        print(f'export BILIBILI_PUSH_URL="{addr}"')
        print('export BILIBILI_PUSH_CODE="<见会话文件 rtmp_code>"')
    return 0


def cmd_stop(args: argparse.Namespace) -> int:
    session, room_id, csrf = _require_authed(args)
    stop_live(session["cookies"], room_id, csrf)
    print("已关闭 Bilibili 直播间")
    return 0


def cmd_update(args: argparse.Namespace) -> int:
    session, room_id, csrf = _require_authed(args)
    update_room(session["cookies"], room_id, csrf, args.title)
    session["title"] = args.title
    save_session(args.session, session)
    print("直播间标题已更新")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="live.py", description="Bilibili 开播工具（移植自 obs-bilibili-stream）")
    parser.add_argument("--session", type=Path, default=DEFAULT_SESSION, help="会话文件路径")
    sub = parser.add_subparsers(dest="cmd", required=True)

    login = sub.add_parser("login", help="扫码登录并保存会话")
    login.add_argument("--timeout", type=int, default=180, help="扫码等待总时长（秒）")
    login.add_argument("--poll-interval", type=int, default=3, help="轮询间隔（秒）")
    login.set_defaults(func=cmd_login)

    status = sub.add_parser("status", help="检查登录状态")
    status.set_defaults(func=cmd_status)

    areas = sub.add_parser("areas", help="列出直播分区（含子分区 ID）")
    areas.set_defaults(func=cmd_areas)

    start = sub.add_parser("start", help="开播并获取 RTMP 地址/推流码")
    start.add_argument("--area", type=int, required=True, help="子分区 ID（见 areas 输出）")
    start.add_argument("--title", default="", help="直播间标题（可选，开播后更新）")
    start.add_argument("--print-export", action="store_true", help="打印 BILIBILI_PUSH_URL 导出语句")
    start.set_defaults(func=cmd_start)

    stop = sub.add_parser("stop", help="关闭 Bilibili 直播间")
    stop.set_defaults(func=cmd_stop)

    update = sub.add_parser("update", help="更新直播间标题")
    update.add_argument("--title", required=True, help="直播间标题")
    update.set_defaults(func=cmd_update)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except BiliError as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\n已取消", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
