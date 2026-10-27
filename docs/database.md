# 价格数据库与统计口径

采用 SQLite + Python 标准库，不需要数据库服务或外部依赖。现已接入官方 US/CA 商品页采集和站内提醒，支持离线导入；Keepa、浏览器扩展及外部通知服务尚未接入。运行方式见 [监控说明](monitoring.md)。

## 数据模型

| 表 | 作用 |
| --- | --- |
| `products` | 品牌、型号、版本/季节与内部 product_key；不同年份或构造版本分开 |
| `variants` | 某产品下的明确颜色和尺码；无尺码属性的品类可使用明确的 `one-size` |
| `retailers` | 零售商身份 |
| `offers` | 商户内商品报价身份，关联变体、市场和币种；US/USD 与 CA/CAD 严格隔离 |
| `price_observations` | 原始价格观测、库存、成色、价格资格、来源与时间 |

所有价格使用整数美分，不用浮点数。表结构见 [schema.sql](../db/schema.sql)。导入器不覆盖既有价格观测；相同来源记录重复导入视为幂等，内容冲突则整批回滚。SQLite 文件本身不是防篡改审计系统，直接 SQL 写入不受导入器的完整校验保护。

模型、颜色、尺码和季节必须有可信匹配；未知字段不能通过猜测制造精确匹配。`product_key` 应包含必要的款式/性别/版本区分，不以商品标题模糊合并。当前没有自动实体匹配算法。

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
PYTHONPATH=src python3 -m arcteryx_prices --db data/prices.sqlite3 init
PYTHONPATH=src python3 -m arcteryx_prices --db data/prices.sqlite3 import /path/to/verified-observations.json
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

## 查询与验证

以下占位符须换成真实导入记录的身份：

```bash
PYTHONPATH=src python3 -m arcteryx_prices --db data/prices.sqlite3 metrics \
  --product-key PRODUCT_KEY --variant-key VARIANT_KEY --market US --currency USD
```

可加 `--as-of 2026-10-26T23:59:59Z` 查询历史时点。不加则使用当前 UTC 时间。

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
```

核心测试覆盖：窗口边界与未来记录、跨店每日最低/抓取频率、US/CA 与型号/尺码隔离、非合格报价排除、UTC 日期、空历史、四舍五入、重复导入、身份冲突和事务回滚。测试中的价格全部为合成数据。

## 保存与后续接入

`data/prices.sqlite3` 是当前实例真实工作库，已包含首次实际采集的报价，不纳入 Git。`db/schema.sql`、`db/monitor.sql` 和计算代码可重建空库；真实数据需要另行备份，不能把 Git 当作价格历史备份。

当前采集器把经过身份、库存和币种验证的事实转换成相同格式，监控表额外保留关注、检查结果与站内提醒。看板启动时会运行到期检查调度器；没有邮件发送服务。每个监控关注使用独立 URL+SKU 身份，尚不自动将不同商户或语言页面合并。
