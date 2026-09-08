#!/bin/bash
# 录像轮播值守 + TikTok 开播自动切转推。
#
# 用法：bash watch.sh <tiktok_username> [replay.sh 参数...]
#
# 行为：先以后台方式跑 replay.sh 播本地文件；每 60 秒探测 TikTok
# 直播源，一旦开播就停轮播、改房间标题、exec 转推（push.sh）。
# 目标未开播时零冲突（push.sh 侧不启动）。
#
# 依赖：yt-dlp（探测，自动携带项目根 cookies.txt）、ffmpeg、BILIBILI 推流环境变量。

set -u

TARGET="${1:-}"
shift || true
if [ -z "$TARGET" ]; then
    echo "用法：$0 <tiktok_username> [replay.sh 参数...]" >&2
    exit 1
fi

# shellcheck disable=SC1090
[ -f ~/.bashrc ] && source ~/.bashrc
# WebUI 管理的推流配置优先于旧的 ~/.bashrc 导出项。
if [ -f "$HOME/.config/bili/push.env" ]; then
    source "$HOME/.config/bili/push.env"
fi

if [ -z "${BILIBILI_PUSH_URL:-}" ] || [ -z "${BILIBILI_PUSH_CODE:-}" ]; then
    # shellcheck disable=SC1090
    eval "$(grep -E '^export BILIBILI_PUSH_(URL|CODE)=' ~/.bashrc 2>/dev/null)" || true
fi

REPLAY_PID=""
cleanup() {
    [ -n "$REPLAY_PID" ] && kill "$REPLAY_PID" 2>/dev/null || true
    pkill -P $$ ffmpeg 2>/dev/null || true
    exit 0
}
trap cleanup SIGINT SIGTERM SIGQUIT

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

COOKIE_ARGS=()
for cand in "$SCRIPT_DIR/../cookies.txt" /home/ubuntu/tiktok/cookies.txt; do
    if [ -f "$cand" ]; then
        COOKIE_ARGS=(--cookies "$cand")
        break
    fi
done

echo "[$(date '+%Y-%m-%d %H:%M:%S')] 轮播值守中，目标 TikTok @$TARGET"
bash "$SCRIPT_DIR/replay.sh" "$@" &
REPLAY_PID=$!

while kill -0 "$REPLAY_PID" 2>/dev/null; do
    if yt-dlp --no-warnings "${COOKIE_ARGS[@]}" --format "best" \
            --get-url "https://www.tiktok.com/@${TARGET}/live" 2>/dev/null | head -n1 | grep -q .; then
        echo "[$(date '+%Y-%m-%d %H:%M:%S')] 检测到 @$TARGET 开播，切转推"
        kill "$REPLAY_PID" 2>/dev/null || true
        wait "$REPLAY_PID" 2>/dev/null || true
        REPLAY_PID=""
        python3 "$SCRIPT_DIR/live.py" update --title "${TARGET} live" || true
        exec bash "$SCRIPT_DIR/push.sh" "$TARGET"
    fi
    sleep 60
done
