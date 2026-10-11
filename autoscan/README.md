# autoscan —— Emby 按路径刷新（AB 的旁挂服务）

像 qBittorrent 那样独立跑一个容器，AutoBangumi 整理完成后把**这一部番的目录**推给它，
它再按路径通知 Emby 扫描 —— 只影响这一部番，不是整库刷新。

```
AB 整理完成 ──POST /triggers/manual?dir=/mnt/goodgirl/<番剧>──▶ autoscan
                                                                 │ 排队 + 合并（默认等 10 分钟）
                                                                 ▼
                                          POST /Library/Media/Updated { "Updates": [{ "path": "E:\\好女孩\\<番剧>" }] }
                                                                 ▼
                                                               Emby
```

## 三个路径视角（对不上就会白扫）

| 视角 | 路径 |
| --- | --- |
| AutoBangumi / 下载器 | `E:\好女孩\<番剧>\Season N` |
| autoscan 容器 | `/mnt/goodgirl/<番剧>/Season N` ← compose 里把 `E:\好女孩` 只读挂到这里 |
| Emby（Windows） | `E:\好女孩\<番剧>` ← 由 `autoscan/config.yml` 里 target 的 rewrite 换回去 |

`autoscan-push` 插件负责第一段换算（`path_from` → `path_to`），autoscan 的 target rewrite
负责第二段（`/mnt/goodgirl/` → `E:\好女孩\`）。

## 文件

| 文件 | 作用 |
| --- | --- |
| `autoscan/config.yml` | autoscan 配置：只开 `manual` 触发器 + 一个 `emby` 目标（URL/Key/rewrite） |
| `docker-compose.yml` 里的 `autoscan` 服务 | 镜像 `cloudb0x/autoscan:latest`，端口 3030，只读挂媒体目录 |
| `config/plugins/local/autoscan-push/` | AB 侧插件：订阅 `torrent.organized` → 推送目录（含测试） |

## 启用步骤

1. **先把 Emby 跑起来** —— autoscan 启动时要读 Emby 的库列表
   （`GET /emby/Library/SelectableMediaFolders`），Emby 不在会直接 FTL 崩溃重启：
   ```
   FTL Failed initialising target error="libraries: ... connection refused" target=emby
   ```
2. 确认 Emby 地址与 API Key（Emby 后台 → 设置 → 高级 → API 密钥），必要时改 `autoscan/config.yml`。
3. 启动：`docker compose up -d autoscan`
4. 手工验证一次（路径用 **autoscan 容器视角**）：
   ```bash
   curl -X POST 'http://127.0.0.1:3030/triggers/manual?dir=/mnt/goodgirl/%E5%86%8D%E8%A7%81%EF%BC%8C%E6%8B%89%E6%8B%89'
   docker logs autoscan | grep 'Scan moved'
   ```
5. 之后 AB 每次整理完成都会自动推（插件已启用：`plugins.enabled["autoscan-push"] = true`）。

## 已经验证到的部分

| 环节 | 结果 |
| --- | --- |
| autoscan 配置解析 | `Initialised targets autoscan=0 emby=1 …`、`Starting server on :3030` ✓ |
| 手动触发器 | `POST /triggers/manual?dir=…` → HTTP 200 ✓ |
| 媒体目录挂载 | 容器内 `/mnt/goodgirl` 能看到 `再见，拉拉`、`恶女不才…`、`花织…` ✓ |
| AB→autoscan 推送 | 插件推真实目录 → autoscan 日志 `Scan moved to processor` + `status=200` ✓ |
| AB 容器到宿主 | `host.docker.internal` 解析为 `192.168.65.254` ✓ |
| autoscan→Emby 调用 | 用桩 Emby（实现 autoscan 用到的那三个接口）验证：`Scan moved to target library="动漫" path="E:\好女孩/再见，拉拉"`，桩收到 `POST /Library/Media/Updated` 且 body 为 `{"Updates":[{"path":"E:\\好女孩/再见，拉拉","updateType":"Created"}]}` ✓✓ |
| 真实 Emby | 待你启动 Emby 后即可（机制已用桩逐接口验证 ✓） |

### ⚠️ 一个必须记住的坑：target rewrite 的 `to` 要以**正斜杠**结尾

autoscan 判断"这个路径属于哪个库"用的是
`HasPrefix(路径, 库路径 + "/")`，而库路径是从 Emby 读来的
（Windows 上是 `E:\好女孩`）。所以 rewrite 必须产出 **`E:\好女孩/…`** 这种
「盘符与目录用反斜杠、库根之后用正斜杠」的混合形式：

* `to: 'E:\好女孩\'` ✗ → 日志刷 `No target libraries found … failed determining library`，
  **每次扫描都被静默跳过**（实测踩过）
* `to: 'E:\好女孩/'` ✓ → `Scan moved to target`，Emby 侧正常收到（Windows 路径两种斜杠都认）

## 注意点

* **项目已停止维护**（作者公告：功能冻结，兼容性坏了不再修）。它能用，但别指望后续更新；
  Emby 哪天改了接口，可能需要自己 fork。
* **`inotify` 触发器在 Windows 上没用** —— Docker Desktop 的 Windows 目录挂载不会把文件系统
  事件传进容器，所以这里用的是「AB 主动推送」的方式，也正好是更准的那种。
* **最小文件年龄**（当前设成 `minimum-age: 1m`）：推送后 autoscan 会等文件"够老"再扫，
  避免扫到正在写入/复制的文件。想更快可以调到 `10s`，想像默认那样保守就用 `10m`。
* **和 AB 内置的 `media-server-refresh` 插件的区别**：内置那个调 `POST /Library/Refresh`
  （**整库**刷新），autoscan 是**按路径**通知 Emby。两者可以共存，但建议只用一套
  （目前内置那个已经在设置页里被关掉了）。
* webhook 目前**没有开认证**（`Webhooks running without authentication`）——只监听本机端口，
  想更稳妥可以在 `config.yml` 里加 `authentication:`，同时给 AB 插件的用户名/密码填上同样的值。

## 推送去重（避免反复惊动 autoscan）

AutoBangumi **每分钟**会把已整理过的种子重新发一遍 `torrent.organized` 事件，所以在插件里做了
**内容去重**：记住每个目录上次推的内容签名（该目录下正片文件路径的有序列表），

* 内容没变 → **不推**（AB 每分钟重发也不会再通知 autoscan 重扫）；
* 目录里多/少了文件（新一集、洗版换文件）→ **立刻推**一次；
* 一直没变化时，也最多 `dedup_hours`（默认 12 小时）兜底重推一次，避免漏掉同名覆盖之类的情况；
  设 `0` = 只在有变化时才推。

第一次运行会把每个目录各推一次，之后安静。
