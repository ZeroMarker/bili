#!/bin/bash

# Load environment variables from ~/.bashrc
if [ -f ~/.bashrc ]; then
    source ~/.bashrc
fi

# 用法说明
if [ -z "${1:-}" ]; then
    echo "用法：$0 <SOOP 直播间 URL>"
    exit 1
fi

SOOP_URL="$1"

# Bilibili 推流地址
if [ -z "${BILIBILI_PUSH_URL:-}" ] || [ -z "${BILIBILI_PUSH_CODE:-}" ]; then
    echo "错误：请先设置 BILIBILI_PUSH_URL 和 BILIBILI_PUSH_CODE"
    exit 1
fi
BILI_RTMP="${BILIBILI_PUSH_URL}${BILIBILI_PUSH_CODE}"

# 依赖检查
for cmd in yt-dlp ffmpeg; do
    if ! command -v "$cmd" &>/dev/null; then
        echo "错误：$cmd 未安装"
        exit 1
    fi
done

# 日志目录
LOG_DIR="./logs"
mkdir -p "$LOG_DIR"

# 信号处理
FFMPEG_PID=""
cleanup() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] 收到退出信号，清理进程..."
    [ -n "$FFMPEG_PID" ] && kill "$FFMPEG_PID" 2>/dev/null || true
    pkill -P $$ ffmpeg 2>/dev/null || true
    exit 0
}
trap cleanup SIGINT SIGTERM SIGQUIT

echo "开始无人值守推流 SOOP ($SOOP_URL) -> Bilibili"

while true; do
    TIMESTAMP=$(date '+%Y-%m-%d %H:%M:%S')
    echo "[$TIMESTAMP] 正在尝试获取直播源..."

    # 获取 SOOP 直播流 URL（使用 yt-dlp，优先选择最佳源）
    STREAM_URL=$(yt-dlp --no-warnings -f "best" --get-url "$SOOP_URL" 2>&1 | grep -v "^\[" | tail -n1) || true

    if [ -z "$STREAM_URL" ] || [[ "$STREAM_URL" == *"ERROR"* ]]; then
        echo "  → 未监测到直播或抓取失败，60 秒后重试..."
        sleep 60
        continue
    fi

    echo "  → 成功获取源地址"
    echo "  → 开始向 B 站推流..."

    LOG_FILE="${LOG_DIR}/ffmpeg_soop_$(echo "$SOOP_URL" | sed 's/[^a-zA-Z0-9]/_/g')_$(date +%Y%m%d).log"

    # 推流命令（优化参数）
    ffmpeg -y \
        -headers "User-Agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/134.0.0.0 Safari/537.36"$'\r\n'"Referer: ${SOOP_URL}"$'\r\n' \
        -rw_timeout 30000000 \
        -reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 15 \
        -i "$STREAM_URL" \
        -c:v libx264 -preset veryfast -tune zerolatency \
        -b:v 2500k -maxrate 2800k -bufsize 5000k \
        -g 50 -keyint_min 25 \
        -profile:v main -level 3.1 \
        -pix_fmt yuv420p \
        -c:a aac -b:a 128k -ar 44100 -ac 2 \
        -f flv \
        -flvflags no_duration_filesize \
        -max_muxing_queue_size 9999 \
        -fflags +genpts+igndts \
        "$BILI_RTMP" \
        2>> "$LOG_FILE" &
    
    FFMPEG_PID=$!
    
    # 等待推流进程
    wait "$FFMPEG_PID"
    EXIT_CODE=$?
    FFMPEG_PID=""

    # 根据退出码判断重试策略
    if [ $EXIT_CODE -eq 0 ]; then
        echo "[$(date '+%Y-%m-%d %H:%M:%S')] 推流正常结束"
    elif [ $EXIT_CODE -eq 137 ]; then
        echo "[$(date '+%Y-%m-%d %H:%M:%S')] 推流进程被强制终止 (OOM?)"
    else
        echo "[$(date '+%Y-%m-%d %H:%M:%S')] 推流中断 (退出码：$EXIT_CODE)"
    fi
    
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] 10 秒后重新抓取..."
    sleep 10
done
