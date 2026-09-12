# Bilibili 推流

把 TikTok 直播源或本地录制 `.mp4` 转推到 B 站直播间。两条使用路径：WebUI（推荐，systemd 双模式互斥管理）或命令行。

| 文件 | 用途 |
|------|------|
| `live.py` | 开播/停播/改标题/查状态（扫码登录，会话存 `.bilibili_session.json`）。移植自 [obs-bilibili-stream](https://github.com/Zarosmm/obs-bilibili-stream)，标准库 + 同目录 vendored Nayuki 二维码库，无新增依赖；来源与许可证见文件头部（上游 GPL-2.0 衍生） |
| `push.sh` | 直播推流：TikTok 直播源 → Bilibili。未开播时每 60 秒轮询，开播自动转推 |
| `replay.sh` | 文件轮播：本地 `.mp4` 按序循环 → Bilibili（默认 `-c copy`，`--encode` 重编码） |
| `watch.sh` | 轮播值守：先播本地文件，每 60 秒探测 TikTok，一开播就停轮播切转推 |
| `soop.sh` | 直播推流：SOOP 直播间 → Bilibili |
| `webui/app.py` | 管理页后端（标准库 only），见下 |
| `systemd/` | `bili-live` / `bili-replay` / `bili-webui` 三个 user unit + `install.sh` |

## 快速开始（命令行）

```bash
python3 live.py login        # 终端显示二维码，Bilibili App 扫码
python3 live.py areas        # 列出分区，记下子分区 ID
python3 live.py start --area 646 --title "频道名" --print-export
bash replay.sh "recordings/tiktok/<频道目录>"       # 或 bash push.sh <tiktok_username>
python3 live.py update --title "新标题"
python3 live.py stop         # 关播（先停推流进程再关）
```

## WebUI（推荐）

独立实现，不依赖 tiktok 仓库。两种推流模式**互斥**（同时最多跑一个，启动一个会自动停掉并 disable 另一个）：

- 直播推流 `bili-live.service`（`push.sh`）
- 文件轮播 `bili-replay.service`（`replay.sh`）

```bash
bash systemd/install.sh   # 装 3 个 user unit，bili-webui 直接 enable --now
```

- 本地：`http://127.0.0.1:8767`（仅回环，无应用层认证）
- 公网：`https://bili.20070809.xyz`（Caddy 反代 + basicauth，与站群同凭证）

管理页可做：启停两种模式、看 unit 状态与 ffmpeg 是否在推、看 journal 日志、开播/停播/改标题。开播后自动把新推流码保存到 `~/.config/bili/push.env`（权限 600）。手动等价操作：`systemctl --user enable --now bili-live.service`（先停另一个）。

API（JSON）：`GET /api/health|status|logs?which=live|replay|webui&tail=`，
`POST /api/mode {mode,target|paths,encode}`、`POST /api/stop`、
`POST /api/room {action:start|stop|update, area?, title?}`。

## 推流密钥来源

转推脚本拼接 `BILIBILI_PUSH_URL` + `BILIBILI_PUSH_CODE`。来源按优先级：WebUI 的 `~/.config/bili/push.env` → 进程环境及
`~/.bashrc` 的 `source` → 兜底直读 `~/.bashrc` 中的导出项（`push.sh`/`replay.sh`/
`watch.sh` 均有该兜底，systemd 下靠它工作）。每次 `live.py start` 推流码都会换：
命令行开播后需用会话文件中的新码更新配置：已有 `~/.config/bili/push.env` 时更新该文件，否则更新 `~/.bashrc`。`--print-export` 不回显完整推流码。WebUI 开播自动同步到独立配置，无需预先编辑 `.bashrc`。

WebUI 支持表单草稿记忆、后台刷新和持续操作反馈。控制请求互斥，另一项操作未完成时返回 409；多页面操作不会同时启动两路推流。升级后运行 `bash systemd/install.sh` 更新 unit，并执行 `systemctl --user restart bili-webui.service` 加载新后端。

```bash
export BILIBILI_PUSH_URL="rtmp://txy3.live-push.bilivideo.com/live-bvc/"
export BILIBILI_PUSH_CODE="your-stream-key"
```

## 各脚本说明

```bash
bash push.sh <tiktok_username>              # TikTok 直播（需登录态主播用 watch.sh，其自动携带 cookies.txt）
bash soop.sh <SOOP直播间URL>                # SOOP 直播
bash replay.sh <文件|目录> [更多...] [--encode] [--dry-run]
bash watch.sh <tiktok_username> [replay.sh 参数...]   # 轮播值守
```
- `replay.sh`：目录按文件名排序展开其中 `*.mp4`，多输入按序连播、播完循环；
  默认 `-c copy`（h264+aac 源几乎不占 CPU）；跨分段分辨率/帧率跳变卡顿时用
  `--encode`（x264 veryfast + aac，640x1280 约占半核）；`--dry-run` 只打印
  ffmpeg 命令（推流码打码）。日志 `./logs/ffmpeg_replay_*.log`，断线 5 秒重推。
- `push.sh`/`soop.sh`：断线 10 秒重抓；日志 `./logs/ffmpeg_{tiktok,soop}_*.log`。

## 录制文件质检

轮播黑屏/卡住多半是片源问题（录制时源卡顿留下空洞或坏帧），播前抽查：

```bash
# 空洞扫描：输出 gap 即冻结时长（>5s 即会冻结）
ffprobe -v error -select_streams v -show_entries packet=pts_time -of csv <file> \
  | awk -F, 'NR>1 && $2-p>5 {print "gap " $2-p "s at " p "s"} {p=$2}'
# 解码扫描：大量 concealing 即黑屏/花屏段
ffmpeg -hide_banner -v error -i <file> -f null - 2>&1 | grep -c "concealing"
```

坏段用 `ffmpeg -ss <秒> -i <file> -c copy <fixed>.mp4` 切掉；多规格混杂用 `--encode`。

## 已知限制（2026-09-06 实测）

- 标题禁 emoji（接口拒收“房间名不能有表情符号”），用纯文本。
- `update` 成功只代表提交进审核：`audit_info.audit_title_reason` 为“进入审核”时
  公示标题保持默认直到过审；含真实艺人名的标题会被判疑似冒充而长期卡审或驳回，
  纯 ASCII 标题约 1 分钟过审。
- 开播表单不带 `build` 参数：全参数签名必回 `-3 签名错误`，去掉后通过；
  偏离已在 `live.py` 注释与 `tests/test_bili_live.py` 回归测试中锁定。
- 开播可能触发人脸验证（接口码 60024/60043），按终端提示扫码完成后再重试。

## 故障排查

- TikTok 判未开播但用户侧在播：机房 IP 可能被 SlardarWAF/GroupBlock 封锁
  （见 tiktok 仓库 `tk/error.md`），以用户侧为准，或从浏览器 Network 面板抓
  `m3u8`/FLV 直链；亦可用 `cookies.txt` 登录态（部分主播需登录才返流）。
- unit 起不来、`203/EXEC`：脚本缺可执行位（`chmod +x`），`systemd-analyze verify` 校验。
- 双推流冲突：同一房间同时只能一路流；`status` 报 `conflict` 时停掉一路。
  旧 hub 托管进程与 systemd 不互通，迁移时先停旧进程（`hub stop <name>`）。
- 看日志：管理页日志区，或 `journalctl --user -u bili-live.service`。

## 待办

- [ ] ai_haneda_0922 抓流：机房 IP 被 TikTok SlardarWAF/GroupBlock 封锁，
  2026-09-07 用正式引擎链（yt-dlp / impersonate / curl_cffi 兜底）复测仍无流
  （账号存在，昵称 羽田 あい）。条件：拿到日本 VPS 或用户侧 `m3u8` 直链。

## 许可证

除另有声明外，本项目使用根目录 `LICENSE` 中的 MIT License。
`live.py` 是 GPL-2.0 上游代码的衍生移植，按 `COPYING.GPL-2.0` 分发；
`qrcodegen.py` 保留其文件头中的 MIT 许可与作者声明。
