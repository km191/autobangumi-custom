# patch4 —— AutoBangumi 4.0 的后端补丁

三个只读挂载的补丁文件（**注释掉 compose 里对应行即回到官方行为**）：

| 文件 | 补丁 |
| --- | --- |
| `database_bangumi.py` | ① 保存季度/集数偏移时清掉检查标记（修「反复提示」）<br>② **跨季匹配**：同一部番的规则接住相邻下一季（半年番不用每季度重建规则） |
| `offset_detector.py` | 放送中的番不再因「集数超过 TMDB 该季集数」被误标记 |
| `offset_scanner.py` | 只用与规则同一季的种子做集数信号 |

## 1. `database_bangumi.py`：保存偏移时清掉检查标记（修掉"反复提示"）

**现象**：偏移弹窗点「应用」→ 保存 → 下次扫描/刷新又弹出来。

**根因**（源码级确认，不是猜）：

* 前端弹窗点「应用」时**从不调用** `POST /apply-offset/{id}`，而是重新跑一次
  `detect-offset`、把建议写进本地表单，再走通用保存 `PUT /api/v1/bangumi/{id}`；
* 而通用保存的模型 `BangumiUpdate` **根本没有** `needs_review` / `suggested_*` 字段，
  `update()` 又是 `model_dump(exclude_unset=True)` 逐字段 setattr —— 所以
  **标记永远清不掉**，`GET /needs-review` 下一轮就把它弹回来；
* 顺带一提：`db.bangumi.apply_offset()` 本身是对的（会写偏移并清标记），
  但前端没接上它。内置前端里 `apply-offset` 这个字符串一次都没出现。

**补丁**：`update()` 在写入后，如果本次保存带了 `season_offset` / `episode_offset`
（说明用户已经对这次提示做过决定），就一并把 `needs_review`、
`needs_review_reason`、`suggested_season_offset`、`suggested_episode_offset` 清空。

## 2. `offset_detector.py`：放送中的番不再因"集数超过 TMDB"被误报

原来只要 `当前集数 > TMDB 该季集数` 就记一条理由；若番还在放送（`Returning Series`）
则给 `medium` 置信度 —— 而扫描器对 high/medium 都会标记，于是**正在播的番几乎必然
被标记**（TMDB 还没更新到最新一集）。现在放送中只记 debug 日志、不作为错位理由；
已完结番仍照旧提示。

## 3. `offset_scanner.py`：只用同一季的种子做集数信号

原来取该规则**所有**种子解析出的最大集数；一条规则里混着多季种子（合集/多季同目录）时
会把集数撑大 → 误报。现在：
* 显式标了别的季的种子**跳过**；
* 没有季标记的种子仍算本季（它们本来就属于这条规则）；
* 调用处把 `bangumi.season` 传进去。

## 验收（都是实测，临时规则用完即删）

| 项 | 结果 |
| --- | --- |
| 保存偏移后标记 | `needs_review: True → False`，`reason`/`suggested_*` 全部清空 ✓ |
| 放送中 E13 vs TMDB 12 集 | `None`（不再标记）✓ |
| 已完结 E13 vs TMDB 12 集 | 仍返回「调整后集数13超出TMDB该季的12集」✓ |
| 同季信号 | 种子 = S1E05 + S2E30 + 无季E07：**不限季 = 30，规则第1季 = 7**（S2E30 被排除）✓ |

## 生成

```bash
# 从镜像取原始文件
docker cp auto_bangumi:/app/module/database/bangumi.py patch4/database_bangumi.py.orig
docker cp auto_bangumi:/app/module/parser/analyser/offset_detector.py patch4/offset_detector.py.orig
docker cp auto_bangumi:/app/module/core/offset_scanner.py patch4/offset_scanner.py.orig
# 差异见同目录 *.patch.txt
```

## 跨季匹配（同一个文件里的第二处补丁）

**背景**：AB 匹配种子到规则时有一条硬条件 —— `release.season is None or bangumi.season == release.season`，
所以半年番第二季（`S02`）永远进不了 `season=1` 的那条规则，只能每季度重建规则；
而删除规则会连同**下载记录**一起删（源码注释：「重新添加后能重新下载」）→ 于是「重头下」。

**补丁**：调用方已经用标题确认是同一部番，这里再放开**相邻下一季**（只 `+1`）：

```python
if release.season is None or bangumi.season == release.season:
    return True
if os.environ.get("AB_STRICT_SEASON_MATCH", "").strip().lower() in ("1", "true", "yes", "on"):
    return False          # 一键回退成官方严格行为
return release.season == bangumi.season + 1
```

* 只放相邻一季 —— 多年后的续作（`S03` 进 `S01`）不会被吞；
* 电影 / 特别篇 / 混合合集 / PV 的原有护栏一律不动；
* 配合 `quality-wash` 的「库中已有保护」，老集数也不会被重复下载。

**一键回退**：给容器加环境变量 `AB_STRICT_SEASON_MATCH=1`，或把 `database_bangumi.py`
这一行挂载注释掉。

**验证**（`test_season_match.py`，在容器里对**已挂载补丁的模块**跑，17 项断言全过）：

```
docker cp patch4/test_season_match.py auto_bangumi:/tmp/test_season_match.py
docker exec -e PYTHONPATH=/app auto_bangumi python /tmp/test_season_match.py
```

关键三项：

| 用例 | 结果 |
| --- | --- |
| 真实入口 `match_bangumi_in_list`：S2 的发布名 + S1 的规则 | 命中 ✓ |
| 同上，但 `AB_STRICT_SEASON_MATCH=1` | 不命中 ✓（回退有效） |
| S1 的发布名 + S1 的规则 | 命中 ✓（原有行为不变） |
