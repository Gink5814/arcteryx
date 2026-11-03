# Gamma MX 与 Sidewinder：美国男款关注清单

这份[可导入配置](../examples/watches.gamma-mx-sidewinder.us.json)覆盖男款 **XL / XXL**，Gamma MX 无帽 Jacket 和连帽 Hoody 均包括，颜色不限。每个页面、颜色和尺码独立建档，共 **20 个已核验的 REI 关注、5 个商品页**，全部为美国、美元、新品。按最新选择使用 `available_or_target`，检查间隔 **15 分钟**，`target_price: null`：首次确认有货就提醒，之后补货再次提醒，目标价稍后再设。

2026-11-03 新增 [Sidewinder 美国官网配置](../examples/watches.sidewinder-official.us.json)：两色 × XL / XXL，共 4 个关注。两份配置合计 **24 个关注、6 个商品页**。官网记录与 REI 记录独立保存，不擅自将不同店的配色名称映射为同一变体。

## Sidewinder 最新实查：2026-11-03

查询及二次确认时间 **10:09–10:13（America/Denver，MST）**，范围为男款 25 周年 Sidewinder Jacket，全新、美国、USD，不含税运。原始变体标识、确认时间和实查结果见 [验证记录](../research/sidewinder-live-validation.json)。

| 来源 | 颜色 | XL | XXL | 当时价格 |
| --- | --- | --- | --- | ---: |
| [美国官网 0804](https://arcteryx.com/us/en/shop/mens/sidewinder-jacket-0804) | Black / Aquaculture AC | 有货 | 有货 | $850 |
| 同上 | Dark Jade / Black | 有货 | 有货 | $850 |
| [REI C06672](https://www.rei.com/product/C06672/arcteryx-sidewinder-jacket-mens) | Aquaculture | 有货 | 缺货 | $850 |
| 同上 | Jade/Black | 缺货 | 有货 | $850 |

官网 4 个真实 SKU 已加入工作库；本次共检查 8 个 Sidewinder 关注，全部成功，6 个有货组合通过独立复核并生成站内提醒，2 个缺货组合继续保留关注。**没有发送邮件或安装个人电脑定时任务。** 报价仍为正价，不能称为新折扣。

折扣渠道先查了 Dealmoon。10 月 29 日的 [Sidewinder 集合帖](https://www.dealmoon.com/cn/starting-from-400-dealmoon-com-freeride-jackets-collection/6246747.html?target_sites=us)涉及女款，集合起价不能套到这件男款；11 月 3 日 08:20 更新的 [REI 低至 5 折帖](https://www.dealmoon.com/cn/up-to-50-off-arcteryx-deals/5942570.html?target_sites=us)未列出 Sidewinder，不能把整站活动当作此款折扣。上述帖子于本次 10:09–10:12 核验。

REI 页面显示正价消费预计 10% 会员奖励；[会员条款](https://www.rei.com/membership)明确该奖励通常在后续年度发放，非保证、非当场立减。因此表中价格不扣除未来奖励。当前 [REI 优惠页](https://www.rei.com/promotions/coupons)的邮件 15% 券仅适用于 REI Co-op 自有品牌，不适用于这里的 Arc’teryx。

Outlet 的美国男装及硬壳两个列表读取成功，但未发现 Sidewinder 链接；Backcountry、Outdoor Gear Exchange、The Ski Monster 的公开搜索也未找到该外套，evo 搜索返回 HTTP 403。这些仅是本次查询范围内未取得有效报价，不能断言所有商家均无货。

## 补充的美国 Outlet 候选

2026-10-27，用户提供了 [Gamma MX Hoody · 美国 Outlet · 8485](https://outlet.arcteryx.com/us/en/shop/mens/gamma-mx-hoody-8485)，并报告页面价格为 **$280**。按这一报价，它比下表 REI 连帽款的已记录价格 $329.73 低 $49.73，应优先核实 XL / XXL 的颜色与库存；不同商品页的配色或版本仍需对应检查。

此前检索遗漏了这条带编号的准确链接，因此原有候选清单不完整。该 URL 已通过程序的美国 Outlet 地址校验，接入时须实时读取颜色、SKU 和 XL / XXL 库存。上面的报价是用户在 2026-10-27 提供的线索，未提供精确查询时刻，不计入价格历史指标。访问情况以每次实际查询结果为准，[最近查询记录](../research/gamma-outlet-latest-query.json)保留成功或失败及时间；取得明确变体前，可执行预设中不会出现猜测的 SKU。

在实际监控电脑执行下面的命令，可一次加入页面中全部明确颜色的 XL、XXL。若页面确实提供两个颜色且每色都有这两个尺码节点，结果为四个关注，缺货组合也会加入；`missing_combinations` 会指出页面未能核验的组合。重复执行保留已有关注设置。

```powershell
py -3 -X utf8 scripts/run-monitor.py --db data/prices.sqlite3 watch-discover https://outlet.arcteryx.com/us/en/shop/mens/gamma-mx-hoody-8485 --sizes XL XXL
```

Gamma MX Jacket 和 Sidewinder 的官网／Outlet 编号链接通过列表搜索取得，不猜编号：

```powershell
py -3 -X utf8 scripts/run-monitor.py discover --search "gamma mx jacket"
py -3 -X utf8 scripts/run-monitor.py discover --search "sidewinder"
```

搜索返回真实链接后，对目标链接执行同样的 `watch-discover ... --sizes XL XXL`。搜索结果只证明列表中存在该链接，价格和库存仍需商品页面核验；未取得的来源不计入已启用关注数量。

## 已核实的来源

以下为 **2026-10-27 中午 12:00（山地时间，18:00 UTC）** 附近读取到的公开价格与库存，不含税运，之后可能变化。直接读取并用现有采集器核验页面中的 SKU、颜色、尺码、USD、商品状态及明确库存；这些是历史快照，不代表当前库存或历史最低价。

| 商品与来源 | XL / XXL 颜色 | 当时价格 | 有货 / 已选组合 |
| --- | --- | ---: | ---: |
| [Gamma MX Hoody · C11803](https://www.rei.com/product/C11803/arcteryx-gamma-mx-hoody-mens) | Vitality/Black Sapphire | $329.73 | 2 / 2 |
| [Gamma MX Jacket · C11804](https://www.rei.com/product/C11804/arcteryx-gamma-mx-jacket-mens) | Solitude | $284.73 | 1 / 2 |
| [Gamma MX Hoody · 235146](https://www.rei.com/product/235146/arcteryx-gamma-mx-hoody-mens) | Black、Black Sapphire Ii、Cloud、Bracken/Mongoose | $400.00 | 8 / 8 |
| [Gamma MX Jacket · 247526](https://www.rei.com/product/247526/arcteryx-gamma-mx-jacket-mens) | Black、Dark Jade | $350.00 | 4 / 4 |
| [Sidewinder Jacket · C06672](https://www.rei.com/product/C06672/arcteryx-sidewinder-jacket-mens) | Aquaculture、Jade/Black | $850.00 | 2 / 4 |

当时共有 **17 个有货、3 个缺货**。Gamma MX Jacket Solitude XL 缺货、XXL 有货；Sidewinder 有货的是 Aquaculture XL 和 Jade/Black XXL。缺货组合仍保留关注，但价格提醒要求确认有货。

此前使用 `new_low` 规则时的[首轮真实检查记录](../research/gamma-mx-sidewinder-live-validation.json)已入库：20 个关注全部检查成功，共读取 5 个页面；当时仅建立初始观测，没有生成新低提醒或发送邮件。该旧记录不改变现在“首次有货即提醒”的新规则。

Sidewinder 页面注明 “Rereleased for its 25th anniversary”，这是重新发布的 Sidewinder Jacket，不能与旧 Sidewinder AR / SV 混为同一价格历史。不同 Gamma MX 商品页、无帽和连帽款的记录也独立保存。

上方 Gamma MX Outlet 链接的具体变体、Gamma MX 官网对应页面仍待核实；Sidewinder 官网已于 2026-11-03 核实并提供独立预设。此前通用路径返回 404 不能证明这些店没有库存。预设不会自动添加未来新出现的颜色或商品链接。

**先在实际运行的电脑验证各来源能否访问。** 云端成功不能保证其他网络也能读取；只导入旧 REI 配置时，REI 被拦会让其 20 个关注失去覆盖。新增官网预设为 Sidewinder 提供另一来源；各域名分别遵守等待期限，正式 Gmail 运行会发送来源故障通知，试运行只预览。其他官网与 Outlet 商品仍需先核实有效页面和具体 SKU 再加入。

## 在 Windows 导入并开始记录

在仓库根目录的 PowerShell 中运行，使用包含 `watch-import` 的版本：

```powershell
py -3 -X utf8 scripts/run-monitor.py --db data/prices.sqlite3 watch-import examples/watches.gamma-mx-sidewinder.us.json
py -3 -X utf8 scripts/run-monitor.py --db data/prices.sqlite3 watch-import examples/watches.sidewinder-official.us.json
py -3 -X utf8 scripts/run-monitor.py --db data/prices.sqlite3 run-once --dry-run
py -3 -X utf8 scripts/run-monitor.py --db data/prices.sqlite3 serve --no-worker --port 8080
```

前两条命令只加入关注配置，不导入价格或发送邮件。重复导入会跳过已有的相同 URL / SKU，保留你调整过的规则、频率、暂停状态和历史；颜色或尺码冲突、无效记录会使整批导入失败。导入不会停用数据库内其他关注，已有示例可以在网页中暂停。

第三条试运行命令无需 Gmail 设置，会实时采集并保存价格。当前规则首次检查有货即可二次确认并生成提醒，试运行只预览邮件；缺货时等待以后补货。配置中没有预填价格，缺货报价也不进入可购买价格的历史指标。

目标价暂不设置。以后可在网页用美元输入，或执行 `watch-update 编号 --target-price 280`；JSON 使用 `"target_price": "280.00"`。`available_or_target` 的库存与目标价条件分别去重，因此已经触发低价提醒后，后续补货仍会再提醒。若低于目标价但仍缺货，邮件明确标为缺货的价格提醒。原 `new_low` 规则仍可选择，只有它需要先建立价格基线。

正式邮件与 Windows 任务计划程序按[邮件及定时说明](email-alerts.md)设置：每 **5 分钟**运行一次 `run-once --notify gmail`，只检查已到期关注，不加 `--force`。正常采集通常约隔 **15–20 分钟**；网站限流和电脑休眠会推迟。同一轮中两份配置共用 6 个页面的初次读取（5 个 REI、1 个官网），触发提醒时另做独立复核。

本地数据库不会随 Git 克隆到另一台电脑；请在实际运行的 Windows 电脑执行导入和首次采集。云端的测试通过不表示个人电脑的定时任务或 Gmail 已经配置完成。
