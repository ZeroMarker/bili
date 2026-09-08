#!/bin/bash
# 用本地录制文件向 Bilibili 推流（循环重播）。
#
# 来源：转推参数（x264/aac/flv）沿用本仓库 push.sh、soop.sh 的既有配置；
# 开播取码见 bili/live.py（移植自 https://github.com/Zarosmm/obs-bilibili-stream）。
#
# 用法：
#   bash replay.sh <文件|目录> [更多文件|目录...] [--encode] [--dry-run]
#
# 说明：
#   - 目录按文件名排序展开其中的 *.mp4；多个输入按给定顺序连播，播完从头循环。
#   - 默认 -c copy（录制文件已是 h264+aac，与 FLV 兼容，几乎不占 CPU）。
#   - 分段分辨率/参数不一致导致卡顿时改用 --encode 重编码。
#   - 先用 `python3 live.py start --area ID` 开播，再跑本脚本推流。

set -u

# Load environment variables from ~/.bashrc
if [ -f ~/.bashrc ]; then
    # shellcheck disable=SC1090
    source ~/.bashrc
fi
# WebUI 管理的推流配置优先于旧的 ~/.bashrc 导出项。
if [ -f "$HOME/.config/bili/push.env" ]; then
    source "$HOME/.config/bili/push.env"
fi

# 非交互 shell 下 ~/.bashrc 头部会提前 return，兜底直读其中的导出项
if [ -z "${BILIBILI_PUSH_URL:-}" ] || [ -z "${BILIBILI_PUSH_CODE:-}" ]; then
    # shellcheck disable=SC1090
    eval "$(grep -E '^export BILIBILI_PUSH_(URL|CODE)=' ~/.bashrc 2>/dev/null)" || true
fi

ENCODE=0
DRY_RUN=0
INPUTS=()
for arg in "$@"; do
    case "$arg" in
        --encode) ENCODE=1 ;;
        --dry-run) DRY_RUN=1 ;;
        -h|--help)
            sed -n '2,14p' "$0" | sed 's/^# \?//'
            exit 0
            ;;
        -*)
            echo "错误：未知选项 $arg" >&2
            exit 1
            ;;
        *)
            INPUTS+=("$arg")
            ;;
    esac
done

if [ "${#INPUTS[@]}" -eq 0 ]; then
    echo "用法：$0 <文件|目录> [更多文件|目录...] [--encode] [--dry-run]" >&2
    exit 1
fi

if ! command -v ffmpeg >/dev/null 2>&1; then
    echo "错误：缺少依赖 ffmpeg" >&2
    exit 1
fi

if [ -z "${BILIBILI_PUSH_URL:-}" ] || [ -z "${BILIBILI_PUSH_CODE:-}" ]; then
    echo "错误：请先设置 BILIBILI_PUSH_URL 和 BILIBILI_PUSH_CODE" >&2
    exit 1
fi
BILI_RTMP="${BILIBILI_PUSH_URL}${BILIBILI_PUSH_CODE}"

# 收集播放列表（统一转绝对路径：ffmpeg 相对路径解析依赖进程 cwd，如 hub 启动时为 /tmp）
FILES=()
for input in "${INPUTS[@]}"; do
    if [ -d "$input" ]; then
        while IFS= read -r f; do
            FILES+=("$(realpath -m "$f")")
        done < <(find "$input" -maxdepth 1 -name '*.mp4' -type f | sort)
    elif [ -f "$input" ]; then
        FILES+=("$(realpath -m "$input")")
    else
        echo "错误：找不到 $input" >&2
        exit 1
    fi
done

if [ "${#FILES[@]}" -eq 0 ]; then
    echo "错误：没有可播放的 mp4 文件" >&2
    exit 1
fi

LIST_FILE=$(mktemp)
trap 'rm -f "$LIST_FILE"' EXIT
for f in "${FILES[@]}"; do
    # 单引号转义后写入 concat 列表
    printf "file '%s'\n" "${f//\'/\'\\\'\'}" >> "$LIST_FILE"
done

echo "播放列表（${#FILES[@]} 个，循环）："
printf '  %s\n' "${FILES[@]}"

FFMPEG_ARGS=(-re -stream_loop -1 -f concat -safe 0 -i "$LIST_FILE")
if [ "$ENCODE" -eq 1 ]; then
    # 重编码：setpts/aresample 重建时间戳，跨分段（VFR/断流空洞）不断流；
    # 参数沿用 push.sh（x264 veryfast + aac）
    FFMPEG_ARGS+=(
        -fflags +genpts+igndts
        -vf "setpts=N/FRAME_RATE/TB" -r 25
        -c:v libx264 -preset veryfast -tune zerolatency
        -b:v 2500k -maxrate 2800k -bufsize 5000k
        -g 50 -keyint_min 25 -profile:v main -level 3.1 -pix_fmt yuv420p
        -c:a aac -b:a 128k -ar 44100 -ac 2
        -af "aresample=async=1"
    )
else
    FFMPEG_ARGS+=(-c copy)
fi
FFMPEG_ARGS+=(
    -f flv -flvflags no_duration_filesize
    -max_muxing_queue_size 9999
    "$BILI_RTMP"
)

if [ "$DRY_RUN" -eq 1 ]; then
    # 预览不输出真实推流码
    masked=("${FFMPEG_ARGS[@]/$BILIBILI_PUSH_CODE/<推流码已隐藏>}")
    printf 'ffmpeg'; printf ' %q' "${masked[@]}"; printf '\n'
    exit 0
fi
LOG_DIR="./logs"
mkdir -p "$LOG_DIR"
LOG_FILE="${LOG_DIR}/ffmpeg_replay_$(date +%Y%m%d).log"

FFMPEG_PID=""
cleanup() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] 收到退出信号，清理进程..."
    [ -n "$FFMPEG_PID" ] && kill "$FFMPEG_PID" 2>/dev/null || true
    pkill -P $$ ffmpeg 2>/dev/null || true
    exit 0
}
trap cleanup SIGINT SIGTERM SIGQUIT

echo "开始循环推流本地文件 -> Bilibili"

while true; do
    ffmpeg -hide_banner "${FFMPEG_ARGS[@]}" 2>> "$LOG_FILE" &
    FFMPEG_PID=$!
    wait "$FFMPEG_PID"
    EXIT_CODE=$?
    FFMPEG_PID=""
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] 推流中断（退出码 $EXIT_CODE），5 秒后重推..."
    sleep 5
done
