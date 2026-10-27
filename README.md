# Arc'teryx 美国价格监控

面向美国的本地监控工作台：粘贴美国官网、美国 Outlet 或 REI 商品链接，读取颜色和尺码，关注目标价、已观测新低或补货。产品范围仅限 **US / USD**。

前端包括关注清单、价格记录、提醒中心和购买指南四个视图。支持名称/颜色/尺码/商家搜索、商家与状态筛选、美元价格排序，以及在页面直接调整目标价、提醒规则和检查间隔。价格记录页另列带原文来源的美国历史促销参考，明确标注是否计入指标。手机端使用紧凑导航与单列卡片。

[查看桌面截图](docs/screenshots/dashboard-desktop.png) · [查看手机截图](docs/screenshots/dashboard-mobile.png) · [历史来源页面](docs/screenshots/history-sources.png)

![连接实际工作数据库的前端](docs/screenshots/dashboard-desktop.png)

- [美国购买经验与攻略：附来源](docs/north-america-buying-guide.md)
- [运行、监控规则与实际验证](docs/monitoring.md)
- [价格数据库和三项历史指标](docs/database.md)
- [首轮工具调研](docs/research.md)
- [五个成熟开源项目的源码比较与采集优化](docs/open-source-monitoring-review.md)
- [优化后的真实采集验证：5 个关注、4 次页面读取](research/resilience-live-validation.json)
- [真实采集验证记录](research/live-monitor-validation.json)
- [当前美国范围验证：5 个关注、12 条历史参考](research/us-scope-validation.json)
- [第三方零售商与历史资料：接入范围和回填条件](docs/retailers-and-history.md)
- [历史促销来源核查](docs/history-source-research.md)

## 启动

Python 3.10+，仅使用标准库，无需安装第三方 Python 包。在仓库根目录运行：

```bash
PYTHONPATH=src python3 -m arcteryx_prices --db data/prices.sqlite3 init
PYTHONPATH=src python3 -m arcteryx_prices --db data/prices.sqlite3 serve --port 8080
```

服务绑定本机 `127.0.0.1:8080`，后台每 30 秒检查调度状态，每个关注默认 60 分钟采集一次。用户可在界面中立即检查、暂停或恢复。服务关闭后不会继续监控；新任务需要重新启动。

临时网络故障会逐步延长重试间隔；商家限流时保存其等待截止时间，重启后继续遵守。立即检查和读取变体也遵守商家的等待要求，卡片会显示下次可检查时间。同一轮中，同一商品页的不同尺码共用初次页面读取，提醒复核仍独立重新请求。

工作台包含美国关注清单、价格历史、覆盖天数及站内提醒。提醒经两次读取确认并去重。尚未连接邮件服务或实现浏览器扩展；本版为单用户本地工具，不支持直接开放到公网。

## 已验证与限制

已真实验证美国官网、美国 Outlet 和 REI 的明确变体。当前工作库保留 5 个美国试点关注，覆盖现货与缺货状态；它们用于验证功能，不代表用户已经选择这些款式。旧版建立的 1 个加拿大关注已停用并从产品界面隐藏，已有记录保留供审计。

美国关注中包含 2 个 REI 试点。已将 12 条 2025—2026 年的真实美国历史促销报道导入工作库的独立参考表，也可用下方命令在新库重建。报道的原帖日期、价格、条件和来源均保留，但不冒充零售商已核验的具体尺码库存。[此前入库与监控验证](research/retailer-history-live-validation.json) 保留当时的完整快照，历史文件中的加拿大记录不表示当前支持范围。

指标记录从实际采集开始，没有补造历史。365 天最低价、90 天每日最低价均值会同时显示实际覆盖天数。历史报道未通过精确变体存档核验，不计入这些指标；本次尚未取得合格的旧变体存档。未来价格、库存和页面结构可能变化，具体快照见验证记录。

关注记录按 URL 和 SKU 独立保存，当前看板不会自动将官网、Outlet、不同语言页面或不同代产品合并。数据库的底层指标支持明确映射后的跨店比较，但自动商品匹配尚未实现。

## 命令行

```bash
# 发现真实页面中的明确变体
PYTHONPATH=src python3 -m arcteryx_prices discover https://arcteryx.com/us/en/shop/mens/beta-jacket-0868
PYTHONPATH=src python3 -m arcteryx_prices discover https://www.rei.com/product/228617/arcteryx-beta-jacket-mens

# 添加一个示例真实商品关注（价格会实时读取；重复关注会被拒绝）
PYTHONPATH=src python3 -m arcteryx_prices --db data/prices.sqlite3 watch-add examples/watch.us.json

PYTHONPATH=src python3 -m arcteryx_prices --db data/prices.sqlite3 watch-list
PYTHONPATH=src python3 -m arcteryx_prices --db data/prices.sqlite3 check --watch-id 1
PYTHONPATH=src python3 -m arcteryx_prices --db data/prices.sqlite3 alerts

# 导入实际取得的历史促销参考（幂等；不增加指标观测）
PYTHONPATH=src python3 -m arcteryx_prices --db data/prices.sqlite3 history-import research/historical-price-evidence.json
PYTHONPATH=src python3 -m arcteryx_prices --db data/prices.sqlite3 history-list
PYTHONPATH=src python3 -m unittest discover -s tests -v
```

`data/prices.sqlite3` 保存在本地并被 Git 忽略。表结构、程序、研究和验证记录纳入版本管理。`examples/observations.example.json` 是合成单元测试示例，只能导入独立测试库；`examples/watch.us.json` 是真实商品的关注配置，不包含伪造价格。

浏览器回归：`node tests/frontend.e2e.cjs`。测试环境需要可用的 Node.js、Playwright 与 Chromium；当前云环境已提供。该测试使用隔离的合成 API 数据，覆盖实际浏览器操作、移动布局、断线、数据缺口和键盘焦点，不改动工作库。应用本身仍不依赖 Node.js。
