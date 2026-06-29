# 运动品类离线生产方案 (V10)

- **index.html** — 全品类排名 + TOP10 选题方案(20万池),可浏览器打开
- **选题_TOP10_2000.json / 选题_TOP10_10000.json** — TOP10 抽样选题(两档配比;原始 topic 字段 + l1/l2/rank)
- **运动数据筛选分类逻辑词库_V10.xlsx** — V10 分类词表(L1 52 / L2,含优先级 + 正则修复)
- **scripts/filter_creator_subscriptions.py** — 运动创作者订阅名单实际筛选脚本(A档可通过内容库质量闸进入)
- **docs/creator-subscription-screening-standard.md** — 运动创作者订阅名单筛选标准(候选池、A档处理、内容库回查、分平台导入)

口径:质检=argus最终快照(修复后,batch e3235f7f) · 供给=已理解40.6万 · 选题池=20万(209,921,排除2000已跑)
公式:综合 = 0.5×良品率_N + 0.3×供给_N + 0.2×通过率_N
