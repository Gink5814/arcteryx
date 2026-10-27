# Gamma MX 与 Sidewinder：美国男款关注清单

这份[可导入配置](../examples/watches.gamma-mx-sidewinder.us.json)覆盖男款 **XL / XXL**，Gamma MX 无帽 Jacket 和连帽 Hoody 均包括，颜色不限。每个页面、颜色和尺码独立建档，共 **20 个关注、5 个 REI 商品页**，全部为美国、美元、新品。规则为 `new_low`（已观测新低），检查间隔为 **15 分钟**，不设目标价。

## 已核实的来源

以下为 **2026-10-27 18:00 UTC** 附近读取到的公开价格与库存，不含税运，之后可能变化。直接读取并用现有采集器核验页面中的 SKU、颜色、尺码、USD、商品状态及明确库存；这些报价不是历史最低价。

| 商品与来源 | XL / XXL 颜色 | 当时价格 | 有货 / 已选组合 |
| --- | --- | ---: | ---: |
| [Gamma MX Hoody · C11803](https://www.rei.com/product/C11803/arcteryx-gamma-mx-hoody-mens) | Vitality/Black Sapphire | $329.73 | 2 / 2 |
| [Gamma MX Jacket · C11804](https://www.rei.com/product/C11804/arcteryx-gamma-mx-jacket-mens) | Solitude | $284.73 | 1 / 2 |
| [Gamma MX Hoody · 235146](https://www.rei.com/product/235146/arcteryx-gamma-mx-hoody-mens) | Black、Black Sapphire Ii、Cloud、Bracken/Mongoose | $400.00 | 8 / 8 |
| [Gamma MX Jacket · 247526](https://www.rei.com/product/247526/arcteryx-gamma-mx-jacket-mens) | Black、Dark Jade | $350.00 | 4 / 4 |
| [Sidewinder Jacket · C06672](https://www.rei.com/product/C06672/arcteryx-sidewinder-jacket-mens) | Aquaculture、Jade/Black | $850.00 | 2 / 4 |

当时共有 **17 个有货、3 个缺货**。Gamma MX Jacket Solitude XL 缺货、XXL 有货；Sidewinder 有货的是 Aquaculture XL 和 Jade/Black XXL。缺货组合仍保留关注，但价格提醒要求确认有货。

导入后的[首轮真实检查记录](../research/gamma-mx-sidewinder-live-validation.json)也已入库：20 个关注全部检查成功，共读取 5 个页面；仅建立初始观测，没有生成新低提醒或发送邮件。

Sidewinder 页面注明 “Rereleased for its 25th anniversary”，这是重新发布的 Sidewinder Jacket，不能与旧 Sidewinder AR / SV 混为同一价格历史。不同 Gamma MX 商品页、无帽和连帽款的记录也独立保存。

此次尚未核实官网或 Outlet 的有效对应商品页；尝试的通用路径返回 404，不能据此断言这些店没有库存。这份配置目前只覆盖上述 REI 页面，也不会自动添加未来新出现的颜色或新商品链接。

## 在 Windows 导入并开始记录

在仓库根目录的 PowerShell 中运行，使用包含 `watch-import` 的版本：

```powershell
py -3 -X utf8 scripts/run-monitor.py --db data/prices.sqlite3 watch-import examples/watches.gamma-mx-sidewinder.us.json
py -3 -X utf8 scripts/run-monitor.py --db data/prices.sqlite3 run-once --dry-run
py -3 -X utf8 scripts/run-monitor.py --db data/prices.sqlite3 serve --no-worker --port 8080
```

第一条命令只加入关注配置，不导入价格或发送邮件。重复导入会跳过已有的相同 URL / SKU，保留你调整过的规则、频率、暂停状态和历史；颜色或尺码冲突、无效记录会使整批导入失败。导入不会停用数据库内其他关注，已有示例可以在网页中暂停。

第二条命令无需 Gmail 设置，会实时采集并保存价格。首次合格观测建立基线，不会冒称新低；之后出现符合规则的新低，才进行第二次确认并生成提醒。缺货报价不会计入可购买价格的历史指标。配置中没有预填的价格，更不会把这次快照填进过去 365 天或 90 天的数据。

正式邮件与 Windows 任务计划程序按[邮件及定时说明](email-alerts.md)设置：每 **5 分钟**运行一次 `run-once --notify gmail`，只检查已到期关注，不加 `--force`。正常采集通常约隔 **15–20 分钟**；网站限流和电脑休眠会推迟。同一轮中这些组合共用 5 个页面的初次读取，触发提醒时另做独立复核。

本地数据库不会随 Git 克隆到另一台电脑；请在实际运行的 Windows 电脑执行导入和首次采集。云端的测试通过不表示个人电脑的定时任务或 Gmail 已经配置完成。
