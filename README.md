# AutoBangumi 4.0 定制包（插件 + 补丁 + autoscan）

把一套已经跑通的 AutoBangumi 改装件打包，方便在**另一台电脑**上复刻：
**4 个本地插件**（择优洗版 / 推送到 autoscan / 入库通知 / 通知中继）
**+ 3 个后端补丁**（季度偏移、跨季匹配）
**+ autoscan 按路径刷新**。

> 本仓库**不含任何密钥、数据库或媒体文件**：所有地址与 token 都是 `<占位符>`，需要你自己填。

---

## 0. 前提

| 需要 | 说明 |
| --- | --- |
| Docker Desktop | 两个容器：`auto_bangumi` 与 `autoscan` |
| AB 镜像版本 | **必须 `4.0.0-beta.2`** —— 三个补丁是照着这个版本的源码改的，换版本要先看 `patches/README.md` |
| qBittorrent | 带 WebUI（AB 的下载器）；**AB 启动时会等它，所以先开 qB** |
| Emby | 可选但强烈建议：入库通知与「库中已有保护」都依赖它 |
| Telegram bot | 可选：入库通知发图、其它事件转发 |

---

## 1. 目标目录布局

```
autobangumi/
├─ docker-compose.yml          ← 用本包的模板，改 <占位符>
├─ patches/                    ← 三个补丁（只读挂载进容器）
├─ config/                     ← AB 配置目录（首次启动自动生成）
│  └─ plugins/local/           ← 四个插件放这里
├─ data/                       ← AB 数据库（自动生成）
└─ autoscan/
   └─ config.yml               ← 由 config.yml.example 改名并填写
```

## 2. 部署步骤

```powershell
# 1) 建目录
mkdir C:\Users\<你>\Documents\autobangumi
cd C:\Users\<你>\Documents\autobangumi

# 2) 取出本仓库内容（把 pack 里的东西铺到当前目录）
git clone https://github.com/<你的账号>/autobangumi-custom.git pack
Copy-Item .\pack\docker-compose.yml .
Copy-Item .\pack\patches .\patches -Recurse
Copy-Item .\pack\autoscan .\autoscan -Recurse

# 3) 先起一次 AB，让它生成 config/ 与 data/
docker compose up -d auto_bangumi
Start-Sleep 20

# 4) 把四个插件放进 AB 的插件目录
New-Item -ItemType Directory -Force .\config\plugins\local | Out-Null
Copy-Item .\pack\plugins\* .\config\plugins\local\ -Recurse -Force

# 5) 改配置（见第 3 节），然后重启
docker compose up -d
```

## 3. 必填配置

### 3.1 `config/config.json`

把 `config-snippets.json` 里 `plugins` 段合并进去（**`allow_unsigned` 必须为 `true`**），并填写：

| 位置 | 要填什么 |
| --- | --- |
| `plugins.options.quality-wash.emby_url` / `emby_token` | Emby 地址与 API Key（后台 → 设置 → 高级 → API 密钥） |
| `plugins.options.emby-ingest-notify.*` | 同上 + Telegram bot token 与 chat id |
| `plugins.options.notify-relay.*` | 同一个 Telegram bot token 与 chat id |
| `plugins.options.autoscan-push.path_from` / `path_to` | 你的媒体根目录（Windows 视角）与它在 autoscan 容器里的挂载路径 |
| `downloader` / `plugins.instances[0].options` | qB 的地址、用户名、密码 |

### 3.2 `autoscan/config.yml`

由 `autoscan/config.yml.example` 改名而来，需要填 Emby 地址、API Key，以及两处路径：

```yaml
targets:
  emby:
    - url: http://<Emby地址>:8096
      token: <Emby API Key>
      rewrite:
        - from: /mnt/media/          # autoscan 容器里的路径
          to: 'E:\<媒体根目录>/'      # Emby 看到的路径；**结尾必须是正斜杠**
```

> ⚠️ `to` 结尾用**正斜杠**：autoscan 用「库路径 + /」做前缀匹配，写成反斜杠会每次都静默跳过（`No target libraries found`）。

## 4. 验证

```powershell
# AB 健康
Invoke-WebRequest http://127.0.0.1:7892/health -UseBasicParsing

# 插件加载情况（能看到四个插件 + 各自开关）
docker logs auto_bangumi --tail 40 | Select-String "已加载"

# autoscan 是否能读到 Emby 的库（Emby 必须已启动）
docker logs autoscan --tail 20 | Select-String "Initialised targets"

# 补丁是否生效
docker exec auto_bangumi python -c "print('AB_STRICT_SEASON_MATCH' in open('/app/module/database/bangumi.py',encoding='utf-8').read())"
```

插件自带的测试都可以在容器里跑（都不需要外网）：

```powershell
docker exec -e PYTHONPATH=/app auto_bangumi python /app/config/plugins/local/quality-wash/test_quality_wash.py
docker exec -e PYTHONPATH=/app auto_bangumi python /app/config/plugins/local/autoscan-push/test_autoscan_push.py
docker exec -e PYTHONPATH=/app auto_bangumi python /app/config/plugins/local/emby-ingest-notify/test_emby_ingest.py
docker exec -e PYTHONPATH=/app auto_bangumi python /app/config/plugins/local/notify-relay/test_notify_relay.py
# 补丁测试（用数据库副本，安全）
docker cp .\patches\test_offset_patches.py auto_bangumi:/tmp/ ; docker exec -e PYTHONPATH=/app auto_bangumi python /tmp/test_offset_patches.py
docker cp .\patches\test_season_match.py auto_bangumi:/tmp/ ; docker exec -e PYTHONPATH=/app auto_bangumi python /tmp/test_season_match.py
```

## 5. 四个插件都做什么

| 插件 | 作用 | 关键开关 |
| --- | --- | --- |
| **quality-wash** | 按字幕组档位择优：同一集只留最好的；更好的版本到达后洗掉旧的；「编辑规则 → 下载顺序」里可调组顺序；「库中已有保护」让删规则重建后不会重头下；画质比较含位深(10bit>8bit)与容器(mkv>mp4)、版本号(v2>v1) | `sweep_dry_run`（默认开=只报告不删）、`known_library_guard`、`skip_unknown_group`、`notify`、`hold_new_subscriptions` |
| **autoscan-push** | 整理完成后把该番剧目录推给 autoscan，由它按路径通知 Emby 扫描（**不是整库刷新**）；按内容去重，不会每分钟重复推 | `cooldown_minutes`、`dedup_hours`、`path_from/path_to` |
| **emby-ingest-notify** | 等 Emby **真正收录**该集后再发通知：AB 官方剧名 + 集标题 + 字幕组 + 类别(bgm 标签) + 剧情 + 海报（Telegram 发图）；Emby 认错番时只提示不改名 | `telegram_token/chat_id`、`wait_seconds`、`publish_inbox` |
| **notify-relay** | 自己控制的 Telegram 渠道：**丢弃** AB 自带的「重命名完成」单集通知，只转发系统事件（洗版、下载失败等） | `drop_episode` |

## 6. 三个补丁都改了什么

| 补丁 | 解决的问题 |
| --- | --- |
| `database_bangumi.py` | ① 保存季度/集数偏移时清掉 `needs_review`（否则弹窗「应用」后还会反复提示）<br>② **跨季匹配**：同一部番的规则接住相邻下一季（半年番不用每季度重建规则；重建会丢下载记录→重头下） |
| `offset_detector.py` | 正在放送的番不再因「集数超过 TMDB 该季集数」被误标记 |
| `offset_scanner.py` | 只用与规则同一季的种子做集数信号，避免跨季污染 |

**一键回退**：注释掉 `docker-compose.yml` 里对应的挂载行即可；跨季匹配还可单独用环境变量关掉：

```yaml
    environment:
      - AB_STRICT_SEASON_MATCH=1   # 回到官方"严格按季匹配"
```

细节见 `patches/README.md`。

## 7. 注意事项（都是踩过的坑）

1. **先开 qBittorrent，再开 AB** —— AB 启动时会等下载器就绪（约 10 次重试），等不到就一直卡着，界面能开但取不到数据。若已卡住：`docker compose restart auto_bangumi`。
2. **便携版 qB 要用它自己的目录做工作目录启动**，否则会读成空配置。
3. **`autoscan` 启动时 Emby 必须在跑**，否则它会 FTL 退出并反复重启。
4. **autoscan 的 target rewrite 结尾要用正斜杠**（见 3.2）。
5. **Telegram 的 token 会出现在 AB 自带渠道的失败日志里**（`Cannot connect to https://api.telegram.org/bot<token>/...`）。别把日志外发；必要时在 BotFather 里 revoke 换一个。
6. **补丁绑定镜像版本**：升级 AB 前先确认这三个文件在新版本里是否变过，必要时重新生成补丁。
