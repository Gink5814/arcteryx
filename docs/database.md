# 价格数据库与统计口径

采用 SQLite + Python 标准库，不需要数据库服务或外部依赖。当前产品范围仅限 US / USD，已接入美国官网、美国 Outlet、REI 商品页采集和站内提醒，支持离线导入；配置后可通过单次命令发送 Gmail 邮件。Keepa、浏览器扩展尚未接入。运行方式见 [监控说明](monitoring.md)。

## 数据模型

| 表 | 作用 |
| --- | --- |
| `products` | 品牌、型号、版本/季节与内部 product_key；不同年份或构造版本分开 |
| `variants` | 某产品下的明确颜色和尺码；无尺码属性的品类可使用明确的 `one-size` |
| `retailers` | 零售商身份 |
| `offers` | 商户内商品报价身份，关联变体、市场和币种；当前入口仅接受 US/USD，保留旧版地区字段供兼容与审计 |
| `price_observations` | 原始价格观测、库存、成色、价格资格、来源与时间 |
| `historical_evidence` | 独立的历史报道/存档证据、原始时间精度、取得时间与内容哈希；仅核验成功后关联价格观测 |

所有价格使用整数美分，不用浮点数。表结构见 [schema.sql](../db/schema.sql)。导入器不覆盖既有价格观测；相同来源记录重复导入视为幂等，内容冲突则整批回滚。SQLite 文件本身不是防篡改审计系统，直接 SQL 写入不受导入器的完整校验保护。

模型、颜色、尺码和季节必须有可信匹配；未知字段不能通过猜测制造精确匹配。`product_key` 应包含必要的款式/性别/版本区分，不以商品标题模糊合并。当前没有自动实体匹配算法。

初始化监控库会停用旧版非美国关注并使其采集锁失效，但不删除关注或价格记录。产品 API、CLI 导入与指标查询仅接受 US / USD；底层 Python 存储函数仍保留旧地区兼容能力，供明确的离线维护使用，不代表应用继续支持加拿大。

关注检查间隔支持 15–1440 分钟，默认 60 分钟。升级时会迁移旧库的 30 分钟下限约束，保留关注身份、历史、检查及通知关联；既有关注不会自动加快。试运行 `run-once --dry-run` 仍将采集结果写入所选数据库，只跳过邮件发送和通知状态更新。

## 三个指标

查询必须指定 `product_key`、`variant_key`、`market`、`currency`。默认跨店比较，可加 `--retailer-key`，或同时指定 `--retailer-key` 和 `--offer-key` 限定单个报价。

只统计 `new + in_stock + public` 的公开单件商品价，不含税、运费、会员条件、优惠码或组合包。二手、翻新、无货和库存未知记录仍可存储，但不进入这三个指标。

- `observed_all_time_min`：截止 `as_of` 的最低合格观测。不是上市以来所有价格的保证。
- `lowest_365d`：`date(as_of) - 364 days` 的 UTC 00:00 起，至 `as_of`，最低合格观测。
- `average_daily_low_90d`：`date(as_of) - 89 days` 的 UTC 00:00 起，至 `as_of`；每个有样本的 UTC 日期先取跨店最低价，再求算术平均。

这两个窗口包含查询当天，使用 UTC 日历日期，而非严格的 365×24/90×24 小时滑窗。`as_of` 当天可以只有部分时间。所有输入时间须带时区并标准化到 UTC。

90 天均价的精确定义为：

```text
daily_low[d] = min(当天同一SKU/市场/币种的合格观测价)
average_daily_low_90d = sum(daily_low[d]) / 有观测日期数量
```

缺失日不补零、不前向填充。比如只有两天有样本，输出就是“两天观测均价”，同时显示 `observed_days=2`、`missing_days=88`，不能宣传为完整 90 天历史。额外抓取同一天相同价格不会增加该日权重，但更密集采集仍可能发现以前漏掉的低价。

`mean_minor` 是美分单位的十进制字符串，保留六位小数；`display_minor` 按 half-up 四舍五入到整数美分。`sum_daily_min_minor` 和天数可用于重算准确比例。最低价还返回具体时间、商户和来源 URL。

没有历史数据时，最低价和均价为 `null`，覆盖天数为 0。`history.proven_lifetime_history` 始终为 `false`。90 天全部日期都有样本仅说明日期覆盖，不证明逐时完整。

`as_of` 过滤的是价格的 `observed_at`。后来获得的历史回填允许 `captured_at` 晚于 `as_of`，因此查询表示历史价格，不表示当时已知的信息；本原型不做无未来信息的交易回测。

## 初始化和真实数据导入

在仓库根目录：

```bash
python3 scripts/run-monitor.py --db data/prices.sqlite3 init
python3 scripts/run-monitor.py --db data/prices.sqlite3 import /path/to/verified-observations.json
```

JSON 顶层必须只有 `observations` 数组。完整结构参考 [合成示例](../examples/observations.example.json)，示例中的商品和价格都不是真实数据，不得导入真实库。

每条记录包含：

| 字段组 | 字段 |
| --- | --- |
| 商品与变体 | `product_key`, `name`, `model_code`, `season`, `variant_key`, `color`, `size` |
| 店铺与市场 | `retailer_key`, `retailer_name`, `offer_key`, `market`, `currency` |
| 价格事实 | `price_minor`, `availability`, `product_condition`, `price_type` |
| 证据 | `observed_at`, `captured_at`, `source_type`, `source_url`；`notes` 可选 |

`source_type` 支持 `retailer_page`、`authorized_feed`、`manual`、`archive`，来源 URL 须为无内嵌凭据的 HTTPS URL。`observed_at` 是来源所对应的价格时间，`captured_at` 是取得该事实的时间，后者不得早于前者。没有真实历史时间的当前页面，不得倒填到过去日期。

不得把 Keepa 的缺货标记、含运费报价、MSRP 或前一次价格伪装成默认合格报价。未来 Keepa 导入器还需处理变化事件的有效区间和授权数据覆盖；当前 CLI 没有声称支持其原始 JSON。

## 有来源的历史参考与存档回填

`import` 是供维护者使用的原始记录导入工具，验证格式和身份一致性，不自动证明来源内容真实。历史报道应使用专门的证据流程：

```bash
python3 scripts/run-monitor.py --db data/prices.sqlite3 history-import research/historical-price-evidence.json
python3 scripts/run-monitor.py --db data/prices.sqlite3 history-list
```

证据文件使用 `schema_version: 1` 和 `evidence` 数组，存储来源、发布时间/观测日期、时间精度、取得时间、金额、市场、商家、条件、摘要与页面 SHA-256。未知 SKU 或库存保留未知。导入幂等且冲突整批回滚；不会修改实时关注或生成提醒，也不会因文件自报可信而加入价格统计。

只有取得精确时间戳的原零售商 Wayback `id_` 存档后，才可针对现有关注执行：

```bash
python3 scripts/run-monitor.py --db data/prices.sqlite3 history-backfill \
  --evidence-key VERIFIED_ARCHIVE_KEY --watch-id WATCH_ID
```

命令重新获取该存档，验证原 URL、快照时间、页面哈希和同一 SKU/颜色/尺码/币种的公开新货现货报价，成功后原子写入观测与核验关联。优惠报道不接受这项转换。当前未取得可通过核验的真实旧变体存档，存档流程用隔离的合成测试验证，不能据此声称已补齐真实历史。范围及来源详见 [第三方与历史说明](retailers-and-history.md)。

## 查询与验证

以下占位符须换成真实导入记录的身份：

```bash
python3 scripts/run-monitor.py --db data/prices.sqlite3 metrics \
  --product-key PRODUCT_KEY --variant-key VARIANT_KEY --market US --currency USD
```

可加 `--as-of 2026-10-26T23:59:59Z` 查询历史时点。不加则使用当前 UTC 时间。

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
```

核心测试覆盖：窗口边界与未来记录、跨店每日最低/抓取频率、旧地区数据与型号/尺码隔离、仅美国入口、旧关注停用保留、非合格报价排除、UTC 日期、空历史、四舍五入、重复导入、身份冲突和事务回滚。测试中的价格全部为合成数据。

## 保存与后续接入

`data/prices.sqlite3` 是当前实例真实工作库，不纳入 Git。`db/schema.sql`、`db/monitor.sql`、`db/history.sql` 和计算代码可重建空库；真实采集数据需要另行备份，不能把 Git 当作价格历史备份。历史参考文件纳入版本管理，可显式导入重建这部分资料。

当前采集器把经过身份、库存和币种验证的事实转换成相同格式，监控表额外保留关注、检查结果与站内提醒。看板默认运行到期检查调度器，`serve --no-worker` 可只管理关注；邮件由 `run-once --notify gmail` 发送，并在 `notification_deliveries` 中保存发送状态。每个监控关注使用独立 URL+SKU 身份，尚不自动将不同商户或语言页面合并。

采集调度状态也持久化：关注的 `consecutive_failures`、`retry_not_before`，检查的 HTTP/重试元数据，以及 `monitor_host_cooldowns` 的来源等待截止时间。它们不参与价格指标，失败和等待不会新增价格观测。迁移为增量列/表修改，保留已有报价、历史来源和停用的旧地区数据。
