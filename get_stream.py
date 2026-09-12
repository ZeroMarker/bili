#!/usr/bin/env python3
"""取 TikTok 直播流（四级兜底，标准库 + curl_cffi，无外部文件依赖）。

方法（按序，首个命中即返回；顺序/语义对齐 tiktok 的 TikTokAdapter）：
  1-4) yt-dlp × {www 主域， m 子域} × {直连， --impersonate chrome}，
       -f b[ext=flv]/best，有 cookies.txt 则附带（单次超时 25s，tiktok 侧 60s，
       这里收紧以免 push 循环/WebUI 验流等太久）。
  5) webcast 兜底（tiktok_fallback.py，本仓库内 vendor）：curl_cffi 直解页面 +
     webcast API（FLV 优先、HLS 兜底）。

用法：python3 get_stream.py <用户名> [--cookies PATH] [--timeout S]
成功 stdout 只打一行 URL（exit 0）；失败 exit 1。诊断一律打 stderr。
调用方（push.sh / webui probe_tiktok）只认 stdout 首行 http。
"""

import argparse
import os
import subprocess
import sys
from pathlib import Path

ATTEMPT_TIMEOUT = 25
STREAM_SCHEMES = ("http://", "https://", "rtmp://", "rtmps://")


def is_stream_url(value: object) -> bool:
    """Return whether *value* is a pull URL supported by ffmpeg."""
    return isinstance(value, str) and value.lower().startswith(STREAM_SCHEMES)


def _run_capture(cmd: list[str], timeout: int) -> str | None:
    """跑一条 yt-dlp 取流命令，成功返回首行 stdout，否则打诊断回 None。"""
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except (subprocess.TimeoutExpired, OSError) as exc:
        print(f"  [超时/执行失败] {cmd[-1]}：{exc}", file=sys.stderr)
        return None
    if r.returncode != 0:
        err = (r.stderr or "").strip().splitlines()
        print(f"  [失败] {cmd[-1]}：{err[-1] if err else f'exit {r.returncode}'}", file=sys.stderr)
        return None
    lines = (r.stdout or "").strip().splitlines()
    if not lines or not is_stream_url(lines[0].strip()):
        print(f"  [失败] {cmd[-1]}：无可用地址", file=sys.stderr)
        return None
    return lines[0].strip()


def _method5(username: str) -> str | None:
    """webcast 兜底（本仓库 tiktok_fallback，无外部文件依赖）。"""
    try:
        from tiktok_fallback import get_stream_url
    except ImportError as exc:
        print(f"  [跳过] 方法5：导入失败（{exc}）", file=sys.stderr)
        return None
    try:
        url = get_stream_url(username)
    except Exception as exc:  # noqa: BLE001 — 兜底链不能被未知异常打断
        print(f"  [失败] 方法5：{exc}", file=sys.stderr)
        return None
    if is_stream_url(url):
        return url
    print("  [失败] 方法5：无可用地址", file=sys.stderr)
    return None


def fetch(username: str, cookies: str, timeout: int) -> str | None:
    """跑完全链，返回流 URL；都失败返回 None。"""
    cookie_args = ["--cookies", cookies] if cookies and Path(cookies).is_file() else []
    urls = (
        f"https://www.tiktok.com/@{username}/live",
        f"https://m.tiktok.com/@{username}/live",
    )
    # 先 FLV 优先（低延迟），再不设 -f 兜 HLS-only 列表（对齐上游 _try_ytdlp_fallback
    # 首变体默认格式行为），最后 webcast 直解。
    for fmt in ("b[ext=flv]/best", None):
        for url in urls:
            for extra in ([], ["--impersonate", "chrome"]):
                cmd = ["yt-dlp", "--no-warnings"]
                if fmt:
                    cmd += ["-f", fmt]
                stream = _run_capture(
                    [*cmd, *extra, *cookie_args, "--get-url", url],
                    timeout,
                )
                if stream:
                    return stream
    return _method5(username)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="取 TikTok 直播流（多级兜底）")
    parser.add_argument("username", help="TikTok 用户名（可带 @）")
    parser.add_argument("--cookies", default=os.environ.get(
        "TK_COOKIES", str(Path.home() / "tiktok" / "cookies.txt")),
        help="Netscape Cookie 文件（不存在则不带）")
    parser.add_argument("--timeout", type=int, default=ATTEMPT_TIMEOUT,
                        help="单次 yt-dlp 超时秒数")
    args = parser.parse_args(argv)
    username = args.username.strip().lstrip("@")
    if not username:
        print("用户名为空", file=sys.stderr)
        return 1
    stream = fetch(username, args.cookies, args.timeout)
    if stream:
        print(stream)
        return 0
    print(f"@{username} 未开播或所有取流方法失败", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
