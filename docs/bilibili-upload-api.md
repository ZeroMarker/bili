# 哔哩哔哩视频上传接口文档

> 整理自 B 站投稿页（member.bilibili.com）与社区逆向资料，用于脚本化投稿。
> 接口均需登录态（Cookie），未登录会返回 403 / -101。

---

## 1. 流程总览

B 站投稿（Web 端）分为 **文件上传** 和 **稿件提交** 两个阶段：

```text
准备登录态 (Cookie / CSRF / WBI)
        │
        ▼
[可选] 封面上传  POST /x/vu/web/cover/up ──────────────► cover_url
        │
        ▼
1. 预上传      GET  /preupload ────────────────────────► endpoint + upload_id
        │
        ▼
2. 分片上传    PUT  https://{endpoint}/{upload_id}?partNumber=...  (循环)
        │
        ▼
3. 合并完成    POST https://{endpoint}/{upload_id}?output=json ──► filename
        │
        ▼
4. 提交稿件    POST /x/vu/client/add ──────────────────► aid / bvid
        │
        ▼
5. [可选] 编辑  POST /x/vu/client/edit
```

**核心要点：**

- 文件走「分片直传」到 CDN 域名（`upos-*.bilivideo.com`），不是 multipart 表单；
- 稿件信息走 `member.bilibili.com` 的 JSON 接口；
- `filename`（第 3 步返回值）是提交稿件时关联视频文件的凭证，必须原样带上。

---

## 2. 前置准备

### 2.1 登录态 Cookie

需要浏览器登录 B 站后，从 Cookie 中提取：

| Cookie 名 | 用途 |
|---|---|
| `SESSDATA` | 登录凭证（核心） |
| `bili_jct` | CSRF Token 来源 |
| `DedeUserID` | 用户 ID |
| `buvid3` / `buvid4` | 设备标识，风控会参考 |

### 2.2 CSRF Token

- 值就是 Cookie `bili_jct` 的值；
- 所有写接口（add / edit / cover/up）都需要带 `csrf` 或 `csrf_token`。

### 2.3 统一请求头

所有请求建议携带：

```http
User-Agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36
Referer: https://member.bilibili.com/platform/upload/video/frame
Origin: https://member.bilibili.com
Cookie: SESSDATA=...; bili_jct=...; DedeUserID=...
```

---

## 3. 接口明细

### 3.1 预上传 `GET /preupload`

获取上传服务器与上传凭证。

```
GET https://member.bilibili.com/preupload
```

| Query 参数 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `os` | string | 是 | 固定 `upos` |
| `upcdn` | string | 是 | 上传 CDN 供应商：`upos`(默认/优刻得)、`ws`(网宿)、`qn`(七牛)、`bda2`(百度)、`kuaishou`(快手) |
| `probe` | int | 否 | 探测模式，0 或 1 |
| `r` | int | 否 | 随机数，防缓存 |

**响应示例：**

```json
{
  "code": 0,
  "message": "0",
  "ttl": 1,
  "data": {
    "OK": 1,
    "endpoint": "upos-sz-mirrorcos.bilivideo.com",
    "uptoken": "c17f...",
    "upos_uri": "upos://upos-sz-mirrorcos.bilivideo.com/1f9a2b3c4d5e6f7a8b9c0d1e2f3a4b5c",
    "bili_version": "3.2.0",
    "tzone": "Asia/Shanghai",
    "bili_checksum": 0
  }
}
```

**字段说明：**

| 字段 | 说明 |
|---|---|
| `endpoint` | 上传 CDN 主机名，后续请求拼接为 `https://{endpoint}/` |
| `upos_uri` | 格式 `upos://{endpoint}/{upload_id}`，`upload_id` 即路径最后一段 |
| `bili_checksum` | 若为 `1`，合并完成后还需做 MD5 校验（见 3.4） |
| `uptoken` | 备用上传令牌，直传模式一般用不到 |

> 备用地址：`https://member.bilibili.com/x/vupre/web/preupload`（部分时期生效），
> 以实际可用为准；`bilibili-API-collect` 维护的官方路径为 `/preupload`。

---

### 3.2 分片上传 `PUT /{upload_id}`

将文件切分成块，逐块直传 CDN。每块一个 PUT 请求。

```
PUT https://{endpoint}/{upload_id}
```

| Query 参数 | 类型 | 说明 |
|---|---|---|
| `partNumber` | int | 分片序号，从 `1` 开始 |
| `uploadId` | string | 预上传返回的 upload_id |
| `chunk` | int | 当前分片字节数 |
| `chunks` | int | 总分片数 |
| `size` | int | 文件总字节数 |
| `start` | int | 当前分片起始偏移（字节） |
| `end` | int | 当前分片结束偏移（`start + chunk`） |
| `total` | int | 文件总字节数（与 `size` 相同） |
| `type` | string | 固定 `upos` |
| `probe` | int | 固定 `1` |

**请求示例（curl）：**

```bash
curl -X PUT "https://${endpoint}/${upload_id}" \
  --data-binary @part_01.bin \
  -G \
  --data-urlencode "partNumber=1" \
  --data-urlencode "uploadId=${upload_id}" \
  --data-urlencode "chunk=4194304" \
  --data-urlencode "chunks=4" \
  --data-urlencode "size=16777216" \
  --data-urlencode "start=0" \
  --data-urlencode "end=4194304" \
  --data-urlencode "total=16777216" \
  --data-urlencode "type=upos" \
  --data-urlencode "probe=1"
```

**响应：**

- 成功：`HTTP 200`，空 body，响应头带 `ETag: "<etag>"`（**带双引号，需去掉**）；
- 每个分片的 etag 保存下来，合并阶段要用。

**分片建议：**

- 单分片 4 MB（`4194304` 字节）为社区通用默认值；
- 单分片上限 128 MB，总分片数不宜过多；
- 支持断点续传：已上传分片可跳过（幂等 PUT）。

---

### 3.3 合并完成 `POST /{upload_id}`

所有分片上传完毕后，通知服务器合并文件，拿到 `filename`。

```
POST https://{endpoint}/{upload_id}?output=json
Content-Type: application/json
```

**请求体（JSON）：**

```json
{
  "uploadId": "1f9a2b3c4d5e6f7a8b9c0d1e2f3a4b5c",
  "chunk": 4194304,
  "chunks": 4,
  "size": 16777216,
  "partNumber": 4,
  "parts": [
    { "partNumber": 1, "eTag": "abc123..." },
    { "partNumber": 2, "eTag": "def456..." },
    { "partNumber": 3, "eTag": "ghi789..." },
    { "partNumber": 4, "eTag": "jkl012..." }
  ]
}
```

| 字段 | 说明 |
|---|---|
| `uploadId` | 预上传返回的 upload_id |
| `chunk` | 最后一个分片字节数 |
| `chunks` | 总分片数 |
| `size` | 文件总字节数 |
| `partNumber` | 最后一个分片序号（= chunks） |
| `parts[].partNumber` | 分片序号 |
| `parts[].eTag` | 对应分片响应头里的 etag（去引号） |

**响应示例：**

```json
{
  "OK": 1,
  "ok": 1,
  "code": 0,
  "filename": "1f9a2b3c4d5e6f7a8b9c0d1e2f3a4b5c.mp4",
  "x-pre-check": 1
}
```

**`filename` 是提交稿件的关键凭证，必须保存。**

---

### 3.4 文件校验 `POST /{upload_id}`（仅 `bili_checksum=1` 时需要）

预上传返回 `bili_checksum: 1` 时，合并完成后追加一次校验请求：

```
POST https://{endpoint}/{upload_id}?output=json
Content-Type: application/json
```

```json
{
  "uploadId": "1f9a2b3c4d5e6f7a8b9c0d1e2f3a4b5c",
  "fileSize": 16777216,
  "fileMd5": "<文件整体 MD5>"
}
```

成功返回 `code: 0`。若缺失此步，后续提交可能被拒。

---

### 3.5 提交稿件 `POST /x/vu/client/add`

文件上传完成后，提交稿件元信息，正式发布。

```
POST https://member.bilibili.com/x/vu/client/add
Content-Type: application/x-www-form-urlencoded
```

**表单参数：**

| 参数 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `copyright` | int | 是 | `1` 自制，`2` 转载 |
| `source` | string | 转载必填 | 转载来源说明 |
| `cover` | string | 推荐 | 封面 URL（可用 3.6 上传获取） |
| `title` | string | 是 | 标题，1–80 字符 |
| `desc` | string | 否 | 简介，0–1000 字符 |
| `tid` | int | 是 | 分区 ID（见第 4 节） |
| `tag` | string | 是 | 标签，最多 12 个，英文逗号分隔 |
| `videos` | string | 是 | **JSON 数组字符串**，见下 |
| `dtime` | int | 否 | 定时发布，Unix 秒级时间戳 |
| `no_disturbance` | int | 否 | `0` 推送通知 / `1` 静默 |
| `csrf` | string | 是 | `bili_jct` Cookie 值 |

**`videos` 数组元素：**

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `filename` | string | 是 | 3.3 返回的完整文件名（含扩展名） |
| `title` | string | 是 | 分 P 标题 |
| `desc` | string | 否 | 分 P 简介 |
| `tid` | int | 是 | 分区 ID |
| `tag` | string | 是 | 标签 |
| `desc_format_id` | int | 否 | 简介格式：`0` 纯文本 / `1` 富文本 |
| `create_time` | int | 否 | 创建时间戳 |

**curl 示例：**

```bash
curl -X POST "https://member.bilibili.com/x/vu/client/add" \
  -H "Referer: https://member.bilibili.com/platform/upload/video/frame" \
  -H "Cookie: SESSDATA=...; bili_jct=..." \
  --data-urlencode "copyright=1" \
  --data-urlencode "title=我的视频标题" \
  --data-urlencode "desc=简介内容" \
  --data-urlencode "tid=17" \
  --data-urlencode "tag=生活,日常" \
  --data-urlencode "cover=https://i0.hdslb.com/bfs/archive/xxxx.jpg" \
  --data-urlencode "videos=[{\"filename\":\"1f9a2b3c4d5e6f7a8b9c0d1e2f3a4b5c.mp4\",\"title\":\"分P标题\",\"desc\":\"\",\"tid\":17,\"tag\":\"生活,日常\"}]" \
  --data-urlencode "csrf=你的bili_jct"
```

**响应：**

```json
{
  "code": 0,
  "message": "0",
  "ttl": 1,
  "data": {
    "aid": 123456789,
    "bvid": "BV1xxxx"
  }
}
```

---

### 3.6 编辑稿件 `POST /x/vu/client/edit`

修改已发布/草稿稿件。参数与 `add` 一致，额外增加：

| 参数 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `aid` | int | 是 | 稿件 av 号 |
| `bvid` | string | 是 | 稿件 BV 号 |

不更换视频文件时，`videos` 可传原文件信息或省略 `filename` 相关字段。

---

### 3.7 封面上传 `POST /x/vu/web/cover/up`

```
POST https://member.bilibili.com/x/vu/web/cover/up
Content-Type: multipart/form-data
```

| 表单字段 | 说明 |
|---|---|
| `file` | 图片文件（jpg/png/webp 等，B 站会压缩处理） |
| `csrf` | `bili_jct` Cookie 值 |

**响应：**

```json
{
  "code": 0,
  "data": {
    "cover_url": "https://i0.hdslb.com/bfs/archive/xxxx.jpg"
  }
}
```

`cover_url` 直接填入 `add` 的 `cover` 参数。

---

## 4. 分区 ID（tid）

获取方式：`GET https://member.bilibili.com/x/web/archive/pre`（需登录），
`data.tids` 为完整分区树。

常用分区参考：

| tid | 分区 | tid | 分区 |
|---|---|---|---|
| 1 | 动画 | 17 | 生活 |
| 2 | 番剧 | 119 | 美食 |
| 3 | 音乐 | 129 | 舞蹈 |
| 4 | 游戏 | 138 | 数码 |
| 5 | 娱乐 | 160 | 时尚 |
| 11 | 知识 | 188 | 影视 |

---

## 5. 错误码速查

| code | 含义 | 处理建议 |
|---|---|---|
| 0 | 成功 | - |
| -101 | 账号未登录 | 检查 Cookie，重新抓取 SESSDATA |
| -111 | CSRF 校验失败 | 核对 `bili_jct` 与 `csrf` |
| -352 | 风控校验失败 | 需 WBI 签名或人机验证，降低请求频率 |
| -400 | 请求参数错误 | 核对参数类型/必填项 |
| -403 | 权限不足 | 检查账号实名/创作者资格 |
| -404 | 接口不存在 | 接口路径已变更，确认版本 |
| -509 | 请求过于频繁 | 退避重试，控制并发 |
| 34005 | 上传文件校验失败 | 重新分片上传或做 bili_checksum 校验 |

HTTP 403（上传阶段）：多为缺少 Cookie / Referer / UA 被风控。

---

## 6. 完整调用示例（bash 流程）

```bash
#!/usr/bin/env bash
set -euo pipefail

COOKIE="SESSDATA=...; bili_jct=...; DedeUserID=..."
BILI_JCT="你的bili_jct"
FILE="video.mp4"
CHUNK_SIZE=$((4 * 1024 * 1024))   # 4MB

UA="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
REF="https://member.bilibili.com/platform/upload/video/frame"

# 1. 预上传
resp=$(curl -sG "https://member.bilibili.com/preupload" \
  -H "User-Agent: $UA" -H "Referer: $REF" -H "Cookie: $COOKIE" \
  --data-urlencode "os=upos" --data-urlencode "upcdn=upos")
endpoint=$(echo "$resp" | python3 -c "import sys,json;print(json.load(sys.stdin)['data']['endpoint'])")
upload_id=$(echo "$resp" | python3 -c "import sys,json;print(json.load(sys.stdin)['data']['upos_uri'].split('/')[-1])")
checksum=$(echo "$resp" | python3 -c "import sys,json;print(json.load(sys.stdin)['data']['bili_checksum'])")
echo "endpoint=$endpoint upload_id=$upload_id"

# 2. 分片上传
total=$(stat -c%s "$FILE")
chunks=$(( (total + CHUNK_SIZE - 1) / CHUNK_SIZE ))
parts_json=""
offset=0
for ((n=1; n<=chunks; n++)); do
  end=$(( offset + CHUNK_SIZE )); [ $end -gt $total ] && end=$total
  chunk_size=$(( end - offset ))
  dd if="$FILE" bs=1 skip=$offset count=$chunk_size of=/tmp/part.bin 2>/dev/null
  etag=$(curl -s -X PUT "https://${endpoint}/${upload_id}" \
    -H "User-Agent: $UA" -H "Referer: $REF" \
    -G --data-urlencode "partNumber=$n" --data-urlencode "uploadId=$upload_id" \
        --data-urlencode "chunk=$chunk_size" --data-urlencode "chunks=$chunks" \
        --data-urlencode "size=$total" --data-urlencode "start=$offset" \
        --data-urlencode "end=$end" --data-urlencode "total=$total" \
        --data-urlencode "type=upos" --data-urlencode "probe=1" \
    --data-binary @/tmp/part.bin -D - -o /dev/null | tr -d '\r' | awk -F': ' '/[Ee][Tt]ag/{print $2}' | tr -d '"')
  parts_json+="{\"partNumber\":$n,\"eTag\":\"$etag\"},"
  offset=$end
done
parts_json="[${parts_json%,}]"

# 3. 合并完成
filename=$(curl -s -X POST "https://${endpoint}/${upload_id}?output=json" \
  -H "User-Agent: $UA" -H "Referer: $REF" -H "Content-Type: application/json" \
  --data "{\"uploadId\":\"$upload_id\",\"chunk\":$chunk_size,\"chunks\":$chunks,\"size\":$total,\"partNumber\":$chunks,\"parts\":$parts_json}" \
  | python3 -c "import sys,json;print(json.load(sys.stdin)['filename'])")
echo "filename=$filename"

# 4. 提交稿件
curl -s -X POST "https://member.bilibili.com/x/vu/client/add" \
  -H "User-Agent: $UA" -H "Referer: $REF" -H "Origin: https://member.bilibili.com" \
  -H "Cookie: $COOKIE" \
  --data-urlencode "copyright=1" \
  --data-urlencode "title=示例标题" \
  --data-urlencode "desc=示例简介" \
  --data-urlencode "tid=17" \
  --data-urlencode "tag=生活,日常" \
  --data-urlencode "videos=[{\"filename\":\"$filename\",\"title\":\"P1\",\"desc\":\"\",\"tid\":17,\"tag\":\"生活,日常\"}]" \
  --data-urlencode "csrf=$BILI_JCT"
```

---

## 7. 注意事项与风控

1. **登录态时效**：`SESSDATA` 有有效期，脚本化前先验证 `https://api.bilibili.com/x/web-interface/nav` 返回 `code: 0`；
2. **请求头完整**：上传阶段缺 `Referer` / `UA` 极易触发 403；
3. **频率控制**：分片并发建议 ≤ 3，提交/编辑接口间隔 ≥ 1s，触发 `-509` 需指数退避；
4. **WBI 签名**：`client/add` 等接口在风控收紧时要求 `w_rid` / `wts` 签名（从 `/x/web-interface/nav` 取 img/sub key 计算 MD5），脚本建议预留该能力；
5. **MD5 校验**：留意 `bili_checksum` 字段，为 `1` 时必须补 3.4 的校验请求；
6. **接口变更**：B 站接口会不定期调整路径或加参数，批量投稿前先小流量验证；
7. **合规**：投稿需遵守 B 站社区规则与创作者协议，禁止用于刷量、侵权内容。

---

## 8. 参考资料

- [bilibili-API-collect](https://github.com/SocialSisterYi/bilibili-API-collect)（B 站接口逆向文档）
- [biliup / biliup-rs](https://github.com/bilibili-helper/biliup-rs)（上传流程参考实现）
