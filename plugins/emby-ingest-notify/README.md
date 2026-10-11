# 入库通知（emby-ingest-notify）

整理完成后**等 Emby 真正收录这一集**，再发一条通知。和宿主自带的「重命名完成」通知的区别：
宿主那条在重命名时就发（只有剧名/季/集 + 海报），这条在**入库确认之后**发，并且带上
集标题、字幕组、类别、剧情，Emby 认错番时还只信 AutoBangumi 的名字。

## 流程

```
torrent.organized（整理完成）
        │
        ├─ 轮询 Emby: GET /emby/Items?Recursive=true&IncludeItemTypes=Episode&Path=<该文件>
        │     查到 → 说明已入库（查不到就在 wait_seconds 内继续等，超时只记日志不发）
        │
        ├─ 取该集：Name（集标题）/ Overview（剧情）/ SeriesId / SeriesName / Genres
        ├─ 取剧集海报：GET /emby/Items/{SeriesId}/Images/Primary
        ├─ 类别：优先 bgm.tv 标签（自动滤掉「2026年7月」这类季度噪声），失败退回 Emby 分类
        │
        ├─ Telegram：sendPhoto（海报 + caption）；发图失败自动降级 sendMessage 纯文本
        └─ 站内通知中心：一条 emby-ingest-notify.ingested 事件
```

## 通知长这样

```
🎬 恶女不才，请多关照 ～雏宫蝶鼠换身传～
📺 S01E11 · 「第 11 话」
🏷 字幕组：喵萌奶茶屋
✨ 类别：奇幻 / 恋爱 / 喜剧
📝 剧情：……
⚠️ Emby 里识别为「我是不才惡女」      ← 仅当 Emby 的名字和 AB 不一致时出现
```

最后一行来自你之前的要求：**名字以 AutoBangumi 为准**，Emby 认成别的番时只做提示。

## 剧名与字幕组从哪来

整理后的文件名已经被 AB 改写成「剧名 SxxEyy」，**不含字幕组**，所以：

* **剧名**：优先用插件的规则缓存（钩子记下的官方名），没有就取「Season N」的上一级目录名
  （AB 用官方名建目录，所以这个兜底是准的）；
* **字幕组**：来自规则（订阅）的 `group_name`。插件在 `torrent.filter` / `title.parsed`
  钩子里把 `bangumi_id → {官方剧名, 字幕组}` 记进自己的 KV，入库时取用。

> 注意：钩子只在**新种子解析时**触发。如果规则被删过、或进程重启后宿主重发已整理种子的
> 事件，缓存里可能没有该番，此时字幕组会缺省（剧名仍能靠目录名兜底）。

## 幂等

`torrent.organized` 是「至少一次」投递（**进程重启后已整理的种子会再发一次**），
所以按「番剧 + 季 + 集」去重，同一集只通知一次（最近 500 条记在插件 KV 里）。

## 配置（设置 → 插件 → 入库通知）

| 项 | 说明 |
| --- | --- |
| `emby_url` / `emby_token` | Emby 地址与 API Key（后台 → 设置 → 高级 → API 密钥） |
| `telegram_token` / `telegram_chat_id` | 留空则**只发站内**，不发 Telegram |
| `wait_seconds` / `poll_interval` | 等入库的最长时间（默认 180 秒）与轮询间隔（10 秒） |
| `include_overview` / `overview_limit` | 是否带剧情、截断长度（默认 180 字，Telegram caption 上限 1024） |
| `include_tags` / `tag_limit` | 是否带类别、最多几个（默认 5） |
| `path_from` / `path_to` | 可选：AB 与 Emby 看到的路径前缀不同时做映射（默认相同） |
| `min_episode` | 只通知不小于这一集的（默认 1；想看全部就设 0） |
| `telegram_api` | Telegram API 基址（测试用） |

## 测试

```bash
docker exec -e PYTHONPATH=/app auto_bangumi \
  python /app/config/plugins/local/emby-ingest-notify/test_emby_ingest.py
```

本地起一个 HTTP 服务同时扮演 Emby 与 Telegram，覆盖文件名解析、正文拼装（含转义、
Emby 认错番、简介截断）、等入库、发图、发图失败降级、去重、超时不发等 40+ 项断言。

## 实测记录（真实环境）

**自动路径**（真实整理事件触发，1 集自动发出）：

```
14:05:53 [emby-ingest-notify] 入库通知已发送：恶女不才，请多关照 ～雏宫蝶鼠换身传～ S01E11
```

**手工验证**（同一代码路径，全部真服务器）：`GET /emby/Items?…&Path=…` 200 → bgm 标签 200 →
`GET /emby/Items/48056/Images/Primary` 200 → `POST api.telegram.org/…/sendPhoto` 200。

## 踩过的三个坑（都已在代码里修掉）

1. **订阅者队列是按顺序执行的** —— 处理器里 `await` 等待入库会把队列堵住：
   实测对照组收到 22 条整理事件，本插件只处理了 1 条。现在处理器**立刻返回**，
   等待放到后台任务（并发上限 8），并用「处理中」集合防止同一集重复发送。
2. **事件路径是正斜杠**（`E:/好女孩/…`），而 Emby（Windows）的 `Path` 过滤只认反斜杠
   —— 只查一次会永远查不到、白等超时。现在两种形式各试一次。
3. **`torrent.filter` 是决策型钩子**，必须返回 `Verdict`（返回 `None` 不合契约）。
   本插件只借它记录规则信息，一律 `Verdict.ok()` 放行。

## 首次运行的基线（不会刷屏）

订阅里已有的整季内容不该被当成"新入库"轰炸一遍，所以**首次启动时可以把库里已有的集
标记为已通知**：往插件 KV（`plugin_kv` 里 `plugin_id='emby-ingest-notify'`、`key='episodes'`）
写入 `["番剧|季|集", …]` 即可，插件启动日志会显示「已通知过 N 集」。
想重新体验一遍补发，把该键清空后重启即可。

## 与宿主自带通知的关系

宿主在**重命名完成**时也会发一条（只有剧名/季/集 + 海报）。两条不冲突但会各发一次；
不想要宿主那条，可以在通知设置里改它的模板或关掉对应渠道。**入库这条不会重复发同一集。**
