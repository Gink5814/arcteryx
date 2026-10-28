# Gamma MX 与 Sidewinder：美国男款关注清单

这份[可导入配置](../examples/watches.gamma-mx-sidewinder.us.json)覆盖男款 **XL / XXL**，Gamma MX 无帽 Jacket 和连帽 Hoody 均包括，颜色不限。每个页面、颜色和尺码独立建档，共 **20 个已核验的 REI 关注、5 个商品页**，全部为美国、美元、新品。按最新选择使用 `available_or_target`，检查间隔 **15 分钟**，`target_price: null`：首次确认有货就提醒，之后补货再次提醒，目标价稍后再设。

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

现已取得上方用户提供的准确 Outlet 商品链接，其具体变体仍待核实；官网的对应页面也仍待核实。此前通用路径返回 404 不能证明这些店没有库存。这份可执行配置目前只覆盖上述 REI 页面，也不会自动添加未来新出现的颜色或新商品链接。

**先在实际运行的电脑验证 REI 能否访问。** 先前云端成功不能保证其他网络也能读取；若 REI 被拦，这份单一来源预设会全部失去价格覆盖。修复后的试运行会显示 `all_sources_blocked: true`、受影响的 20 个关注和共享重试时间；正式 Gmail 运行会发送一封来源故障通知，试运行只预览。官网与 Outlet 需先核实有效页面和具体 SKU，再加入这份关注清单。

## 在 Windows 导入并开始记录

在仓库根目录的 PowerShell 中运行，使用包含 `watch-import` 的版本：

```powershell
py -3 -X utf8 scripts/run-monitor.py --db data/prices.sqlite3 watch-import examples/watches.gamma-mx-sidewinder.us.json
py -3 -X utf8 scripts/run-monitor.py --db data/prices.sqlite3 run-once --dry-run
py -3 -X utf8 scripts/run-monitor.py --db data/prices.sqlite3 serve --no-worker --port 8080
```

第一条命令只加入关注配置，不导入价格或发送邮件。重复导入会跳过已有的相同 URL / SKU，保留你调整过的规则、频率、暂停状态和历史；颜色或尺码冲突、无效记录会使整批导入失败。导入不会停用数据库内其他关注，已有示例可以在网页中暂停。

第二条命令无需 Gmail 设置，会实时采集并保存价格。当前规则首次检查有货即可二次确认并生成提醒，试运行只预览邮件；缺货时等待以后补货。配置中没有预填价格，缺货报价也不进入可购买价格的历史指标。

目标价暂不设置。以后可在网页用美元输入，或执行 `watch-update 编号 --target-price 280`；JSON 使用 `"target_price": "280.00"`。`available_or_target` 的库存与目标价条件分别去重，因此已经触发低价提醒后，后续补货仍会再提醒。若低于目标价但仍缺货，邮件明确标为缺货的价格提醒。原 `new_low` 规则仍可选择，只有它需要先建立价格基线。

正式邮件与 Windows 任务计划程序按[邮件及定时说明](email-alerts.md)设置：每 **5 分钟**运行一次 `run-once --notify gmail`，只检查已到期关注，不加 `--force`。正常采集通常约隔 **15–20 分钟**；网站限流和电脑休眠会推迟。同一轮中这些组合共用 5 个页面的初次读取，触发提醒时另做独立复核。

本地数据库不会随 Git 克隆到另一台电脑；请在实际运行的 Windows 电脑执行导入和首次采集。云端的测试通过不表示个人电脑的定时任务或 Gmail 已经配置完成。
