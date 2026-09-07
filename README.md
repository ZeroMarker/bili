# Bilibili 推流

转推脚本从 `~/.bashrc` 读取推流地址和密钥（`BILIBILI_PUSH_URL` +
`BILIBILI_PUSH_CODE` 拼接；非交互 shell 下 `~/.bashrc` 头部提前 return
时，`replay.sh` 会兜底直读其中的导出项）：

```bash
export BILIBILI_PUSH_URL="rtmp://example/live-bvc/"
export BILIBILI_PUSH_CODE="your-stream-key"
```

## 自动开播

`live.py` 移植自 [obs-bilibili-stream](https://github.com/Zarosmm/obs-bilibili-stream)
的扫码登录/开播逻辑（标准库 + 同目录 vendored 的 Nayuki 二维码库，无新增依赖）。
完整来源与许可证说明见 `live.py` 头部文档（该文件为上游 GPL-2.0 代码的衍生移植）：

```bash
python3 live.py login        # 终端直接显示二维码，Bilibili App 扫码；会话存 .bilibili_session.json（600，已忽略）
python3 live.py status       # 检查登录态
python3 live.py areas        # 列出分区，记下子分区 ID
python3 live.py start --area 646 --title "今晚直播" --print-export
python3 live.py update --title "新标题"
python3 live.py stop         # 关闭 Bilibili 直播间
```

`start` 成功后把 `rtmp_addr`/`rtmp_code` 写回会话文件（推流码不打终端）；
`--print-export` 给出 `BILIBILI_PUSH_URL` 导出语句。

已知限制（2026-09-06 实测）：

- 直播间标题不能含 emoji（B 站接口拒绝并提示“房间名不能有表情符号”），用纯文本。
- `update` 返回成功只代表提交进审核：响应体 `audit_info.audit_title_reason`
  为“进入审核”时，公示标题保持默认直到过审，需调 `Room/get_info` 确认，
  不要只看返回码。含真实艺人名的标题（如偶像本名）会被判疑似冒充而长期
  卡审或驳回；纯 ASCII 标题约 1 分钟过审。推他人直播流本就踩盗播/录播红线，
  标题避开真人姓名。
- 开播表单不带 `build` 参数：与上游一致的全参数签名必回 `-3 签名错误`，
  去掉 `build` 后签名通过；该偏离已在 `live.py` 注释与
  `tests/test_bili_live.py` 回归测试中锁定。
- 开播可能触发人脸验证（接口码 60024/60043），按终端提示扫码完成后再重试。

## 直播源转推

```bash
bash push.sh <tiktok_username>      # TikTok 直播
bash soop.sh <SOOP直播间URL>        # SOOP
bash twitch/twitch.sh <twitch_username>  # Twitch（仓库根旁路脚本）
bash yt.sh <YouTube handle|直播链接>     # YouTube（仓库根旁路脚本）
```

## 本地录制文件轮播

把已录制的 `.mp4` 循环推到 Bilibili（无人值守重播）。先开播，再跑推流：

```bash
bash replay.sh "recordings/tiktok/<频道目录>"   # 目录按文件名排序连播，播完循环
bash replay.sh a.mp4 b.mp4                     # 也可传多个文件/目录混排
```

默认 `-c copy`（录制文件已是 h264+aac，几乎不占 CPU）；录制文件含断流空洞
或跨分段卡顿时加 `--encode` 重编码（`setpts`/`aresample` 重建时间戳，
640x1280 约占半核）。`--dry-run` 只打印 ffmpeg 命令（推流码打码）不推流。
日志写入 `./logs/ffmpeg_replay_*.log`，断线 5 秒后自动重推。

完整开播→推流→停播（标题禁 emoji，用纯文本）：

```bash
python3 live.py login
python3 live.py start --area 646 --title "频道名" --print-export
bash replay.sh "recordings/tiktok/<频道目录>" --encode
python3 live.py update --title "新标题"
pkill -f replay.sh
python3 live.py stop
```

切片源：先改标题，再重启推流（开播状态保持）：

```bash
python3 live.py update --title "新频道名"
pkill -f replay.sh
bash replay.sh "recordings/tiktok/<新频道目录>" --encode
```

## 轮播值守（录像打底 + 开播自动切直播）

`watch.sh` 先循环播本地文件，同时每 60 秒探测 TikTok 是否开播；
一开播就停轮播、改房间标题、转推直播源：

```bash
bash watch.sh <tiktok_username> "recordings/tiktok/<频道目录>" --encode
```

目标未开播时零冲突（只轮播 + 轮询）。注意 TikTok 机房 IP 可能被
SlardarWAF/GroupBlock 封锁导致误报未开播（见 tiktok 仓库 `tk/error.md` (https://github.com/ZeroMarker/tiktok/blob/main/tk/error.md)），此时以
用户侧能看为准，或从浏览器 Network 面板抓 `m3u8` 直推。

## 录制文件质检

轮播黑屏/卡住多半是片源问题（录制时源卡顿留下空洞或坏帧），与竖屏横屏无关。
播前抽查 packets 空洞（>5s 即会冻结）与解码错误：

```bash
# 空洞扫描：输出 gap 即冻结时长
ffprobe -v error -select_streams v -show_entries packet=pts_time -of csv <file> \
  | awk -F, 'NR>1 && $2-p>5 {print "gap " $2-p "s at " p "s"} {p=$2}'
# 解码扫描：大量 concealing 即黑屏/花屏段
ffmpeg -hide_banner -v error -i <file> -f null - 2>&1 | grep -c "concealing"
```

确认损坏的文件（如数分钟坏头、数十秒空洞）直接删除或用
`ffmpeg -ss <秒> -i <file> -c copy <fixed>.mp4` 切掉坏段后再播；
同目录多规格混杂（分辨率/帧率跳变）时用 `--encode` 统一重整。

## 待办

- [ ] systemd 自活：当前推流靠 hub detached 进程（随会话无关，但机器重启后停）。
  根治需参照 `systemd/` 现有套路加 user service。条件：确认需要 24/7 不间断。
- [ ] ai_haneda_0922 抓流：机房 IP 被 TikTok SlardarWAF/GroupBlock 封锁，
  文档 5 种方法 + 引擎兜底链均失败。条件：拿到日本 VPS 或用户侧 `m3u8` 直链。
