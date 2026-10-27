# Arc'teryx 北美价格监控

面向美国和加拿大的本地监控工作台：粘贴官方商品链接，读取颜色和尺码，关注目标价、已观测新低或补货。美国 USD、加拿大 CAD 独立统计。

前端包括关注清单、价格记录、提醒中心和购买指南四个视图。支持名称/颜色/尺码搜索、市场与状态筛选、按币种分组排序，以及在页面直接调整目标价、提醒规则和检查间隔。手机端使用紧凑导航与单列卡片。

[查看桌面截图](docs/screenshots/dashboard-desktop.png) · [查看手机截图](docs/screenshots/dashboard-mobile.png)

![连接实际工作数据库的前端](docs/screenshots/dashboard-desktop.png)

- [北美购买经验与攻略：14 个已读取来源](docs/north-america-buying-guide.md)
- [运行、监控规则与实际验证](docs/monitoring.md)
- [价格数据库和三项历史指标](docs/database.md)
- [首轮工具调研](docs/research.md)
- [真实采集验证记录](research/live-monitor-validation.json)

## 启动

Python 3.10+，仅使用标准库，无需安装第三方 Python 包。在仓库根目录运行：

```bash
PYTHONPATH=src python3 -m arcteryx_prices --db data/prices.sqlite3 init
PYTHONPATH=src python3 -m arcteryx_prices --db data/prices.sqlite3 serve --port 8080
```

服务绑定本机 `127.0.0.1:8080`，后台每 30 秒检查调度状态，每个关注默认 60 分钟采集一次。用户可在界面中立即检查、暂停或恢复。服务关闭后不会继续监控；新任务需要重新启动。

工作台包含关注清单、US/CA 筛选、价格历史、覆盖天数及站内提醒。提醒经两次读取确认并去重。尚未连接邮件服务或实现浏览器扩展；本版为单用户本地工具，不支持直接开放到公网。

## 已验证与限制

2026-10-26（America/Denver）已真实验证美国官网、加拿大官网和美国 Outlet 的明确变体。工作库中建立了 4 个试点关注，采到 3 个现货报价和 1 个缺货报价；它们用于验证不同地区和库存状态，不代表用户已经选择这些款式。

首批记录从本次采集开始，没有补造历史。365 天最低价、90 天每日最低价均值会同时显示实际覆盖天数。未来价格、库存和页面结构可能变化，具体快照见验证记录。加拿大 Outlet 尚未完成真实验证。

关注记录按 URL 和 SKU 独立保存，当前看板不会自动将官网、Outlet、不同语言页面或不同代产品合并。数据库的底层指标支持明确映射后的跨店比较，但自动商品匹配尚未实现。

## 命令行

```bash
# 发现真实页面中的明确变体
PYTHONPATH=src python3 -m arcteryx_prices discover https://arcteryx.com/us/en/shop/mens/beta-jacket-0868

# 添加一个示例真实商品关注（价格会实时读取；重复关注会被拒绝）
PYTHONPATH=src python3 -m arcteryx_prices --db data/prices.sqlite3 watch-add examples/watch.us.json

PYTHONPATH=src python3 -m arcteryx_prices --db data/prices.sqlite3 watch-list
PYTHONPATH=src python3 -m arcteryx_prices --db data/prices.sqlite3 check --watch-id 1
PYTHONPATH=src python3 -m arcteryx_prices --db data/prices.sqlite3 alerts
PYTHONPATH=src python3 -m unittest discover -s tests -v
```

`data/prices.sqlite3` 保存在本地并被 Git 忽略。表结构、程序、研究和验证记录纳入版本管理。`examples/observations.example.json` 是合成单元测试示例，只能导入独立测试库；`examples/watch.us.json` 是真实商品的关注配置，不包含伪造价格。

浏览器回归：`node tests/frontend.e2e.cjs`。测试环境需要可用的 Node.js、Playwright 与 Chromium；当前云环境已提供。该测试使用隔离的合成 API 数据，覆盖实际浏览器操作、移动布局、断线、数据缺口和键盘焦点，不改动工作库。应用本身仍不依赖 Node.js。
