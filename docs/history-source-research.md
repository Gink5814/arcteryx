# 美国历史价格补充：证据、范围与缺口

本轮核验日期：2026-10-27 UTC。已取得 **12 条真实、带发布日期的美国历史优惠报道**，覆盖 2025-01-31 至 2026-10-01；来源为 Slickdeals 的社区或员工帖子，涉及 REI 与 Saint Bernard。记录保存在 [`research/historical-price-evidence.json`](../research/historical-price-evidence.json)，逐来源核验过程保存在 [`research/history-source-audit.json`](../research/history-source-audit.json)。

这些报道能帮助用户了解某个系列过去出现过的价格，但还不能证明当前关注的具体颜色、尺码、SKU 与版本当时也能以该价格购买。因此，导入后用于“历史优惠参考”，**不会改变具体关注项的历史最低、365 天最低或 90 天均价**。

## 已核验的部分记录

下列日期均把原帖带时区的发布时间换算为 UTC；金额是原文明确写出的商品价格，未用折扣百分比反算，也没有采用站点展示时四舍五入的整数价格。

| 原帖发布日期（UTC） | 商品及范围 | 报道商家 | 报道价格 | 来源 |
| --- | --- | --- | --- | --- |
| 2026-10-01 | 男款 Beta，2 个促销颜色，尺码未明确 | REI | USD 249.83 | [原帖](https://slickdeals.net/f/20083488-arc-teryx-men-s-beta-gore-tex-3-layer-jacket-2-colors-249-83-free-shipping) |
| 2026-10-01 | 男款 Beta SL，3 个促销颜色，尺码未明确 | REI | USD 299.83 | [原帖](https://slickdeals.net/f/20083569-arc-teryx-men-s-beta-sl-gore-tex-shell-jacket-3-colors-299-83-free-shipping) |
| 2026-10-01 | 男款 Atom Insulated Hoody，4 个促销颜色 | REI | USD 199.83 | [原帖](https://slickdeals.net/f/20083530-arc-teryx-men-s-atom-insulated-hoody-4-colors-199-83-free-shipping) |
| 2026-08-04 | 男款 Gamma MX，Spotlight 或 Dk Stratus/Fluidity；原帖提醒尺码不一 | REI | USD 199.83 | [原帖](https://slickdeals.net/f/19842387-arc-teryx-mens-gamma-mx-hoody-50-off-2-color-options-199-83) |
| 2026-05-13 | 女款 Beta AR，Tatsu / Olive Moss | REI | USD 389.83 | [原帖](https://slickdeals.net/f/19517205-arc-teryx-women-s-beta-ar-jacket-tatsu-olive-moss-389-83-free-shipping) |
| 2026-02-27 | 男款 Atom Insulated Hoody，多个颜色且对应尺码不同 | REI | USD 225.83 | [原帖](https://slickdeals.net/f/19256122-arc-teryx-atom-insulated-hoody-men-s-vitality-soulsonic-dynasty-euphoria-225-83) |
| 2026-01-26 | 男款 Atom Jacket，Black；需使用 SAINTBERNARD20 优惠码 | Saint Bernard | USD 224.00，**优惠码价** | [原帖](https://slickdeals.net/f/19139896-arc-teryx-men-s-atom-jacket-black-s-m-l-xl-xxl-free-shipping-price-w-20-off-coupon-224) |
| 2025-09-10 | 男款 Beta SL，Electra；未提供尺码 SKU | REI | USD 350.93 | [原帖](https://slickdeals.net/f/18595150-arc-teryx-men-s-beta-sl-jacket-350-93-rei) |
| 2025-09-10 | 男款 Atom Insulated Hoody，Soulsonic / Trail Magic，XL–XXXL | REI | USD 179.83 | [原帖](https://slickdeals.net/f/18594790-arc-teryx-men-s-atom-insulated-hoody-2-colors-179-83-free-shipping) |
| 2025-01-31 | 男款 Rho Heavyweight Zip-Neck，Black；正文后来标注 L 缺货 | REI | USD 99.83 | [原帖](https://slickdeals.net/f/18093367-arc-teryx-rho-heavyweight-zip-neck-base-layer-top-men-s-black-99-83) |

其余两条为 2026-10-01 的女款 Squamish（Shincha，USD 99.83）与男款 Incendo Hybrid Hoody（Black，USD 129.83），完整链接及原文摘录见数据文件。表中的不同 Atom / Beta 记录不得直接连成同一条价格曲线：型号修订、颜色、尺码和可买库存未完成严格匹配。

## 来源可信度如何处理

- 直接读取优惠帖正文和页面数据，不把搜索结果摘要当作历史成交价。原文支持金额与商家；每条保留简短原文摘录、来源 URL、作者信息、取回时间和完整响应的 SHA-256。
- Slickdeals 是社区／员工整理的优惠平台，页面声明含联盟营销内容。它可作为可追溯的历史报道来源，但不是零售商当时网页或付款凭证；票数、员工身份、是否上首页都不能代替价格核验。
- `observed_at` 在这批 `deal_report` 中表示原帖的 `createdAt` 发布时间；`captured_at` 表示本次读取时间。**原帖可能被编辑，发布时间不证明正文中每个价格或库存说明在该秒已经存在。** 精确时间来自源站字段，没有人为补午夜时间。
- 库存与商品成色保守记录为 `unknown`；原帖列出的尺码、颜色范围仅放进说明，不展开成一批假想 SKU。优惠码价独立标记为 `coupon`，不会计入公开价格指标。
- USD 来自美国商家与美国优惠帖语境；当前只研究美国报价，不进行换汇。价格未合并税费、运费或会员权益。2026-02-27 的 Atom 帖提到会员免运费，该条件在说明中保留。

## 本轮未能补齐的历史

| 渠道 | 实际访问结果 | 处理 |
| --- | --- | --- |
| Internet Archive / Wayback CDX、archive.org 可用性接口 | 代理 CONNECT 返回 403，未读取到存档 | 没有声称存在任何可用快照，也没有伪造存档时间或价格 |
| GearJunkie、Gear Patrol、Switchback Travel | 代理 CONNECT 返回 403 | 未将文章标题、猜测路径或经验印象写成数据 |

没有取得完整的 90 天或 365 天连续历史，也没有取得能直接补入当前关注项的精确变体零售商存档。这批记录是可审计的历史线索；后续只有取回同时证明日期、型号版本、颜色／尺码 SKU、币种、价格条件与库存的原始记录，才能进入具体变体的价格统计。没有证据的日期持续留空，不前向填充，不把零散优惠帖解释为全站或全生命周期最低价。

来源审计 JSON 保留此前调查过的其他地区及失败请求，供追溯过程使用；当前范围已收窄为美国，加拿大来源不再是待补齐目标。
