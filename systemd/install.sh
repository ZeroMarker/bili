#!/bin/bash
# 安装 bili 双模式 systemd user service + webui（无需 root）。
#
# 装什么：
#   bili-live.service    直播推流（push.sh），需经 webui 或手动写 live.env 后启动
#   bili-replay.service  文件轮播（replay.sh），需经 webui 或手动写 replay.env 后启动
#   bili-webui.service   管理页（127.0.0.1:8767），本脚本直接 enable --now
#
# 两种推流模式互斥（同时只能跑一个），由 webui 保证；手动操作时自己先停另一个。

set -u

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
UNIT_DIR="$HOME/.config/systemd/user"
CONF_DIR="$HOME/.config/bili"

if ! command -v systemctl >/dev/null 2>&1; then
    echo "错误：找不到 systemctl" >&2
    exit 1
fi

install -d -m 700 "$CONF_DIR" "$UNIT_DIR" "$PROJECT_ROOT/logs"

# Unit 模板不能假定仓库位于 ~/bili。替换真实路径，并使用 systemd
# 支持的 C 风格转义保护空白及其他特殊字符。
escape_unit_value() {
    local value="$1"
    value="${value//\\/\\x5c}"
    value="${value// /\\x20}"
    value="${value//$'\t'/\\x09}"
    value="${value//$'\n'/\\x0a}"
    value="${value//\"/\\x22}"
    value="${value//\'/\\x27}"
    value="${value//%/%%}"
    value="${value//&/\\&}"
    printf '%s' "$value"
}

ESCAPED_PROJECT_ROOT="$(escape_unit_value "$PROJECT_ROOT")"
for unit in bili-live.service bili-replay.service bili-webui.service; do
    rendered="$(mktemp "$UNIT_DIR/.${unit}.XXXXXX")"
    while IFS= read -r line || [ -n "$line" ]; do
        printf '%s\n' "${line//@PROJECT_ROOT@/$ESCAPED_PROJECT_ROOT}"
    done < "$SCRIPT_DIR/$unit" > "$rendered"
    chmod 644 "$rendered"
    mv -f "$rendered" "$UNIT_DIR/$unit"
done
for pair in "live.env:TARGET=" "replay.env:REPLAY_ARGS="; do
    file="${pair%%:*}"; key="${pair#*:}"
    if [ ! -e "$CONF_DIR/$file" ]; then
        {
            echo "# 由 systemd/install.sh 生成（600，不进 git），webui 会改写"
            echo "${key}"
        } > "$CONF_DIR/$file"
        chmod 600 "$CONF_DIR/$file"
    fi
done
if [ ! -e "$CONF_DIR/webui.env" ]; then
    install -m 600 "$SCRIPT_DIR/webui.env.example" "$CONF_DIR/webui.env"
fi

systemctl --user daemon-reload
if ! systemd-analyze verify "$UNIT_DIR/bili-webui.service" 2>/dev/null; then
    echo "警告：unit 校验有报错，继续安装" >&2
fi

if [ "$(loginctl show-user "$USER" -p Linger --value 2>/dev/null)" != "yes" ]; then
    if sudo -n true 2>/dev/null; then
        sudo loginctl enable-linger "$USER"
        echo "已开启 linger（重启后用户服务自启）"
    else
        echo "提示：需 sudo loginctl enable-linger $USER 才能在重启后自启，请手动执行一次。"
    fi
fi

systemctl --user enable --now bili-webui.service
systemctl --user --no-pager --full status bili-webui.service | head -8
echo "管理页：http://127.0.0.1:8767 （以 BILI_WEBUI_PORT 为准）"
echo "推流模式经管理页启动（互斥）；手动：systemctl --user enable --now bili-live.service"
