# 运动创作者订阅筛选标准

更新时间: 2026-06-29

本文沉淀运动创作者订阅名单的筛选口径。核心原则是: 原榜单只作为候选池, 最终能否进入订阅取决于内容库回查后的活跃度、质量和误判风险。

对应实现: `scripts/filter_creator_subscriptions.py`。

## 目标

订阅系统的目标是稳定获得新内容, 不是保存历史高热视频作者。因此筛选标准优先回答三个问题:

1. 这个作者是否真的是运动相关?
2. 这个作者近期是否仍在更新?
3. 平台 profile 是否能被稳定订阅和增量拉取?

## 数据来源

- 候选池: `enum_v5.json` 生成的 YouTube / Instagram 运动创作者候选。
- 内容库回查: `asset_center.assets` 中的全库视频记录。
- 主键: `platform + author_id`。
- 回查字段: 全库视频数、有效播放视频数、最新发布时间、中位播放、中位互动率、最近标题样本、平台 category 样本。
- 输入归一化: 脚本会把平台码统一为 `yt/ig/tk`, trim profile uid, 并把发布时间规范成 `YYYY-MM-DD` 后再比较。

不得把抽样中的 `n` 直接等同于作者真实质量。抽样 `n` 只代表原候选榜对该作者的证据深度。

## 候选池准入

候选池来自标题规则和质量闸:

- `n >= 2`
- 有效播放率 `valid_n / n >= 0.5`
- 运动垂直度 `vert >= 0.6`
- 能归到一个主运动类目 `dom_l1`
- 中位播放 `med_views >= 5,000`
- 中位播放 `med_views <= 50,000,000`
- 排除内容农场、明显非运动、纯舞蹈默认不订

候选池只能称为 candidate/catalog, 不能直接称为最终订阅名单。

## A 档处理

A 档不等于低质量。A 档只说明原抽样证据浅, 常见原因是样本中视频数较少或类目票数不足。

最终订阅判断中:

- S 档和 A 档都可以进入。
- A 档必须通过全库回查的活跃度和质量闸。
- 不应仅因为 A 档而剔除。
- 不应按原榜单分数把 A 档高互动小样本无复核地批量导入。

实际执行时, A 档通过全库回查后进入同一订阅池或 canary 池; 未通过回查则进入复核/剔除池。

## 内容库回查质量闸

对每个候选作者, 用全库记录重新聚合:

- `full_n`: 内容库内该作者总视频数。
- `valid_n`: 播放数大于等于 100 的视频数。
- `valid_rate = valid_n / full_n`。
- `recent_pub`: 内容库内最新发布时间。
- `med_views`: 有效播放视频的中位播放。
- `med_eng`: 有效播放视频的中位互动率, 公式为 `min((likes + comments + shares) / views, 1.0)`。

进入可订阅或 canary 的硬条件:

- `valid_rate >= 0.5`
- `recent_pub >= 2025-12-29` 即近 6 个月有新作
- `med_views >= 5,000`
- 无明显非运动/娱乐硬负例
- profile uid 可用

近 30 天活跃不是硬门槛, 但应作为优先级字段:

- `active30 = recent_pub >= 2026-05-30`
- active30 作者优先导入和观察。

## 硬负例

以下情况进入复核/剔除池:

- 作者名或标题显示明显不是运动创作者, 例如影视、音乐、游戏、书评、纯娱乐账号。
- 平台 category 只落在 `Music`、`Gaming`、`Film & Animation` 等非运动类别。
- 标题命中运动词但语义不是真运动, 例如歌曲名、电影片段、游戏教程。

注意: 不要过度依赖作者名关键词。`NFL Films`、`Courtside Films` 这类体育影像账号不应仅因为包含 `Films` 被剔除。

## 分平台执行口径

### YouTube

YouTube profile/channel ID 稳定, 可作为首批订阅主线。

进入条件:

- 候选池通过。
- 内容库回查通过质量闸。
- S 或 A 均可进入。

建议导入顺序:

1. active30 且 S/A 通过质量闸。
2. 近 6 个月活跃且 S/A 通过质量闸。
3. 其余进入复核池。

### Instagram

Instagram 可作为 canary, 不应与 YouTube 同权批量导入。

进入 canary 条件:

- 候选池通过。
- 内容库回查通过质量闸。
- S 或 A 均可进入。
- profile handle / author_id 可解析。

上线前必须先跑小批量 canary, 记录:

- profile 是否可访问
- 是否私密或 404
- 是否触发限流
- 是否能拿到 recent media
- handle 是否发生变化

### TikTok

当前 TikTok 不应直接进入正式订阅名单。

原因:

- 原订阅候选榜没有覆盖 TikTok。
- 内容库中虽有 TikTok author 信息, 但 profile 订阅 label / uid 稳定性仍需验证。
- 现阶段只能作为 discovery / watchlist。

TikTok watchlist 可以用标题运动命中、活跃度、播放质量先粗筛, 但必须标记为 `profile_unverified`。

## 输出分层

最终交付应至少分四个文件或表:

- `youtube_subscription_ready`: 可直接导入的 YouTube 订阅池。
- `instagram_canary_ready`: Instagram canary 池。
- `tiktok_watchlist`: TikTok 观察池, 不直接导入。
- `review_or_reject`: 不活跃、低质量、误判或证据不足的复核/剔除池。
- `current_subscription_state`: 当前全量状态, 用于下一轮动态刷新。
- `import_actions`: 本轮需要执行的新增、恢复、暂停或移除动作。

## 动态刷新策略

线上不是一次性名单, 而是周期刷新:

1. 每日或每周重新读取候选池与内容库回查结果。
2. 用同一套准入闸重新计算平台决策。
3. 与上一轮 `current_subscription_state.csv` 对比。
4. 输出本轮 `import_actions.csv`。
5. 导入系统只消费 action, 不直接消费全量候选池。

状态迁移:

- `new`: 首次进入可前进名单。
- `retained`: 上轮已在可前进名单, 本轮仍通过。
- `reactivated`: 上轮在 review, 本轮重新通过。
- `downgraded`: 上轮可前进, 本轮因不活跃、低质量或误判进入 review。
- `missing_downgraded`: 上轮可前进, 本轮候选池或回查结果中未出现, 需要暂停或移除。
- `still_review`: 连续留在复核/剔除池。
- `missing_review`: 上轮已在 review, 本轮仍未出现, 继续保留复核状态。

生命周期:

- `active_subscription`: YouTube 正式订阅。
- `canary`: Instagram 灰度订阅。
- `watchlist`: TikTok 观察池。
- `paused_review`: 曾经进入订阅/canary, 本轮降级, 需要暂停或移除。
- `rejected_review`: 未通过准入闸, 暂不进入订阅。

导入动作:

- `upsert_subscription`: 新增或恢复 YouTube 订阅。
- `upsert_canary`: 新增或恢复 Instagram canary。
- `upsert_watchlist`: 新增或恢复 TikTok watchlist。
- `pause_or_remove`: 已有订阅源本轮不再通过, 需暂停或移除。
- `keep_subscription` / `keep_canary` / `keep_watchlist`: 无需导入动作, 只保留状态。
- `hold_review`: 留在复核/剔除池。

## 运行方式

若已有内容库回查审计 CSV:

```bash
python scripts/filter_creator_subscriptions.py \
  --audit-csv path/to/existing_subscription_list_audit.csv \
  --previous-state out/subscriptions/current_subscription_state.csv \
  --run-date 2026-06-29 \
  --active-months 6 \
  --output-dir out/subscriptions
```

若需要直接从内容库回查 `enum_v5.json` 候选:

```bash
ASSET_CENTER_DSN='postgres://...' \
python scripts/filter_creator_subscriptions.py \
  --candidates-json path/to/enum_v5.json \
  --previous-state out/subscriptions/current_subscription_state.csv \
  --run-date 2026-06-29 \
  --active-months 6 \
  --output-dir out/subscriptions
```

输出文件:

- `all_subscription_audit.csv`
- `youtube_subscription_ready.csv`
- `instagram_canary_ready.csv`
- `tiktok_watchlist.csv`
- `review_or_reject.csv`
- `platform_summary.csv`
- `current_subscription_state.csv`
- `import_actions.csv`

## 2026-06-29 审计结论

基于全库回查, 原 yt/ig 候选池共有 4,556 个作者:

- YouTube 原候选 1,760 个。
- Instagram 原候选 2,796 个。

最保守的 S-only 首批口径:

- YouTube 可直接 seed: 429 个。
- Instagram 可进 canary: 673 个。

允许 A 档在通过内容库质量闸后加入时:

- YouTube 可扩展到 783 个。
- Instagram 可扩展到 1,975 个。

因此最终建议采用扩展口径: A 档可以加入, 但必须通过活跃度、有效播放率、播放下限和误判检查。

按 2026-06-29 的日历 6 个月口径重跑状态机:

- 首次生成: `import_actions` 为 2,758 条, 即 783 个 YouTube 订阅 upsert + 1,975 个 Instagram canary upsert。
- 同日带 `previous-state` 回放: `import_actions` 为 0 条, 证明不会重复导入。
- 模拟上一轮 ready 账号本轮从候选池消失: 生成 1 条 `pause_or_remove`。

## 验收标准

筛选方案通过前必须满足:

- 每个平台单独出名单, 不混排。
- A/S 的含义写清楚, 不把 A 当作质量失败。
- 新鲜度使用全库 `recent_pub`, 不使用抽样下界。
- 至少抽检 top 作者标题样本, 确认无明显非运动污染。
- Instagram 和 TikTok 必须标记 canary / watchlist, 不得与 YouTube 同权导入。
- 文档中不得包含数据库密码、API key 或其他敏感连接信息。
