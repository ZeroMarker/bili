#!/bin/bash

# Load environment variables from ~/.bashrc
if [ -f ~/.bashrc ]; then
    source ~/.bashrc
fi

USERNAME="${1:-}"
if [ -z "$USERNAME" ]; then
    echo "用法：$0 <TikTok 用户名>"
    exit 1
fi

for cmd in yt-dlp ffmpeg; do
    if ! command -v "$cmd" >/dev/null 2>&1; then
        echo "错误：缺少依赖 $cmd"
        exit 1
    fi
done

if [ -z "${BILIBILI_PUSH_URL:-}" ] || [ -z "${BILIBILI_PUSH_CODE:-}" ]; then
    echo "错误：请先设置 BILIBILI_PUSH_URL 和 BILIBILI_PUSH_CODE"
    exit 1
fi

# Bilibili 推流地址
BILI_RTMP="${BILIBILI_PUSH_URL}${BILIBILI_PUSH_CODE}"

# 日志目录
LOG_DIR="./logs"
mkdir -p "$LOG_DIR"

echo "开始无人值守推流 TikTok @$USERNAME -> Bilibili"

while true; do
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] 正在尝试获取直播源..."

    # 1. 动态抓取最新的 m3u8 地址
    STREAM_URL=$(yt-dlp --format "best" --get-url "https://www.tiktok.com/@${USERNAME}/live" 2>/dev/null | head -n1)

    if [ -z "$STREAM_URL" ]; then
        echo "  → 未监测到直播或抓取失败，60 秒后重试..."
        sleep 60
        continue
    fi

    echo "  → 成功获取源，开始向 B 站推流..."

    LOG_FILE="${LOG_DIR}/ffmpeg_tiktok_${USERNAME}_$(date +%Y%m%d).log"

    # 2. 核心推流逻辑 (修复版 v3)
    # 修复点:
    # - 使用 setpts 滤镜重新生成视频时间戳
    # - 使用 aresample=async=1 重新生成音频时间戳
    # - 保持 48000Hz 音频采样率
    # - H.264 重新编码确保兼容性
    ffmpeg -re \
        -headers "User-Agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/134.0.0.0 Safari/537.36"$'\r\n'"Referer: https://www.tiktok.com/"$'\r\n' \
        -fflags +genpts+igndts+discardcorrupt \
        -reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5 \
        -i "$STREAM_URL" \
        -vf "setpts=N/FRAME_RATE/TB" -r 25 \
        -c:v libx264 -preset ultrafast -tune zerolatency -b:v 2500k -maxrate 2500k -bufsize 5000k -g 50 \
        -c:a aac -b:a 128k -ar 48000 -ac 2 \
        -af "aresample=async=1" \
        -f flv \
        -flvflags no_duration_filesize \
        -max_muxing_queue_size 9999 \
        "$BILI_RTMP" \
        2>> "$LOG_FILE"

    echo "[$(date '+%Y-%m-%d %H:%M:%S')] 推流中断，10 秒后重新抓取..."
    sleep 10
done
