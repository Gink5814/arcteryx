# Arc'teryx price alert 首轮调研档案

后续已完成 [五个成熟开源项目的源码比较与采集优化](open-source-monitoring-review.md)，包括 PriceBuddy、changedetection.io、webchanges、urlwatch 和 Huginn；该报告记录本轮实际采用的稳定性改进。

> **当前产品范围仅限美国 US / USD。** 本页保留最初北美调研的历史记录，其中加拿大项目、候选商家和网络要求不属于当前计划。现在已接通美国官网、美国 Outlet 和 REI，见 [运行与验证](monitoring.md)、[美国购买攻略](north-america-buying-guide.md) 和 [历史来源研究](history-source-research.md)。下文“本轮”、零数据状态及网络阻挡只描述首轮调查，不代表当前实现或连通性。

调研日期：2026-10-26（America/Denver）。来源访问时间使用 UTC，见 `research/sources.json`。这是首轮实证调研；“已读源码”“官网待核实”和“已跑通零售站”分别记录。

## 结论

已找到针对美国和加拿大 Outlet 的开源提醒工具，但主要能力是比较前后两次页面状态，不能直接提供完整的一年价格历史。没有证据证明某个已核实插件能同时给出北美各店、同一颜色尺码的历史最低、365 天最低和 90 天均价。

Keepa 的官方 API 模型确实提供 Amazon 历史序列与价格统计，可以作为 Amazon 商品的补数候选。它不证明 Arc'teryx 的具体 ASIN 有完整覆盖，也不能代替品牌官网和其他零售商的数据。自有数据库应保存原始观测，再按统一口径计算指标。

因此建议采用“自有价格历史库 + 可替换采集适配器 + 提醒界面”的结构。此次完成研究和数据库原型；尚未开发浏览器扩展、采集器或通知服务。

## 插件与通用工具

| 候选 | 已取得的证据 | 北美与历史数据判断 | 当前结论 |
| --- | --- | --- | --- |
| [Keepa](https://keepa.com/) | 官方 [README][keepa-readme]、[Stats][keepa-stats]、[Product][keepa-product] 与地区枚举 | API 有 US/CA 地区、ASIN/domainId、`trackingSince`、`csv`；`min` 是已记录历史最低，`minInInterval` 是指定区间最低，`avg90` 是时间加权均价 | 可评估 Amazon 专项历史补数。需 API 授权与额度；未取得商品数据、未验证 Arc'teryx ASIN 或商业套餐。浏览器扩展版本本次未核实 |
| [changedetection.io][changedetection] | 项目维护者 README 描述单商品页价格/补货检测、价格图表、阈值和百分比提醒、Browser Steps、REST API | 可以作为从开始监测时积累观测的后端候选；README 不能证明追踪前的历史覆盖，也未实测 Arc'teryx 页面 | 值得做采集适配试验；它是通用监测服务，不是已验证的 Arc'teryx 专用插件 |
| [Distill](https://distill.io/) | 官网被代理 CONNECT 403 拦截；未找到足够权威的替代文档 | 当前店铺支持、浏览器插件、历史保留、导出/API 均待核实 | 保留候选，不按记忆填写功能或价格 |
| [Visualping](https://visualping.io/) | 同上 | 同上 | 待核实 |
| [CamelCamelCamel](https://camelcamelcamel.com/) | 同上 | Amazon 地区覆盖、ASIN 历史和导出条件本次未核实 | 待核实，不视为已获得历史源 |
| [Karma](https://www.karmanow.com/)、[PayPal Honey](https://www.joinhoney.com/) | 同上 | 需核实 US/CA 商家覆盖、变体识别、历史期限、数据导出和商业使用条件 | 待核实 |
| [ShopSavvy](https://shopsavvy.com/)、[Price.com](https://price.com/) | 同上 | 需核实真实 Arc'teryx 商品覆盖和历史接口 | 待核实 |

Chrome Web Store 的检索请求也被当前策略阻止，尚未核实商店中的发布者身份、插件版本、安装量和权限。因此以上不是安装推荐清单，也不能据此断言不存在其他 Arc'teryx 专用插件。

Keepa 的 `avg90` 与本仓库的“每日跨店最低价的算术平均”不是同一个指标。其时间序列还包含缺货标记 `-1`，部分价格类型含运费。适配器必须先区分价格类型、缺货区间与覆盖范围，不能把汇总值或变化事件直接冒充逐日实测价格。

## Arc'teryx 专项开源项目

以下只阅读了公开文档和代码，没有运行第三方代码，也没有读取其线上数据库。引用固定在核实的提交，便于后续复核。

| 项目 | 实际用途 | 历史价格限制与采用判断 |
| --- | --- | --- |
| [lethalgem/arcteryx-outlet-tracker][us-worker] | US Outlet；Cloudflare Worker、降价/上新检测、尺码过滤、Resend 邮件。仓库有 MIT LICENSE | [`inventory.ts`][us-inventory] 覆盖同一个 KV 快照，只能比较前后状态。README 写每 30 分钟，但 [`wrangler.toml`][us-cron] 实际每日 17:00 UTC。尺码提取失败时仍可能保留商品，不能认定目标尺码一定有货。可参考实现思路，不是历史价格源 |
| [TongTong0828/arcteryx-monitor][ca-monitor] | CA Outlet 男装；新品、价格变化、下架、邮件通知 | README 宣称最近 100 条历史，但 [`monitor.py`][ca-code] 实际覆盖 `baseline.json`，比较页面价格字符串。当前代码不能证明有可用的完整历史库。根目录未见 LICENSE，不复制代码 |
| [kzhao5/arcteryx-stock-monitor][stock-monitor] | README 针对 Leutia Pant、白色、00S 的补货通知 | 属于库存监控；没有证据支持三个价格统计指标。根目录未见 LICENSE |
| [wantai-dev/001-arcteryx-deals-platform][deals-schema] | schema 有 `price_history`，提醒脚本支持目标价/任意降价；材料包含 US/CA | 有历史表的设计，但没有核实真实价格数量、连续覆盖、SKU 匹配或数据复用授权。未查询其 Supabase，也未导入其价格。根目录未见 LICENSE，仅参考概念 |

## 零售数据范围

首轮曾研究 US/USD 与 CA/CAD 的全新商品公开单件价格；此处保留调查范围供审计。当前范围已收窄为 US/USD。比较仍按实际型号版本、颜色、尺码进行，不混入二手、会员价、折扣码、组合包、税费和运费。

| 数据源候选 | 用途 | 本轮访问与验证状态 |
| --- | --- | --- |
| Arc'teryx 主站 US/CA | 当前商品、型号和直接售价 | 主站请求被代理拦截；未验证页面、当前价格或可用接口 |
| Arc'teryx Outlet US/CA | 折扣价和历史观测的优先试点 | Outlet 请求被代理拦截；第三方监控源码说明其目标路径，但不证明当前抓取有效 |
| Arc'teryx ReGEAR | 可另建二手价格轨道 | 仅候选，未请求；二手状况与新货分开，不进入默认指标 |
| REI / Backcountry | 美国零售源候选 | REI 请求被代理拦截；Backcountry 未请求。尚未核实当前库存、品牌授权、变体和历史接口 |
| Altitude Sports / MEC | 加拿大零售源候选 | 未请求；需逐店确认，不能据候选名单认定当前有货 |

尚无零售站采集成功记录，数据库里也没有商品价格观测。商品页划线价/MSRP 只能表示参考原价，不能替代历史成交价或历史最低价。

## 三个指标的口径

| 用户需求 | 当前实现 | 解释 |
| --- | --- | --- |
| 历史最低 | `observed_all_time_min` | 同一可比商品范围中，已记录且符合资格的最低价；返回时间、店铺和来源。它是“已观测历史最低”，不宣称覆盖商品上市以来所有交易 |
| 过去 365 天最低 | `lowest_365d` | 查询日及之前共 365 个 UTC 日历日，截止 `as_of` 时刻的最低观测价；同时返回覆盖天数 |
| 过去 90 天平均价格 | `average_daily_low_90d` | 暂定为每天先取符合条件的跨店最低价，再对有观测的日期求算术平均。避免抓取次数多的店铺或日期权重更高 |

“90 天均价”的业务定义是本轮默认选择，尚不是用户确认的唯一口径。查询也可限制单店或单个 offer；若以后需要时间加权或按店平均，应新增独立指标，不覆写当前定义。缺失日不补零、不自动延续上一价格；必须同时展示样本天数和缺失天数。每天有一个样本也不意味着全天价格完整。

详见 [数据库说明](database.md)。

## 首轮产物与当时阻碍（历史记录）

已完成：一手来源和内容哈希、候选目录、SQLite 表结构、离线 JSON 导入、三项指标与核心测试。真实数据库已初始化，价格观测数为 0；合成数据只用于测试。

首轮之后已完成美国官方来源及 REI 变体验证、持续采集和站内提醒。当前后续工作聚焦美国第三方覆盖及可核验的美国历史存档，加拿大接入已移出计划。抓取失败要保留为失败，不能写成缺货、零价或历史低价。若以后选择 Keepa，仍需先验证具体美国 ASIN 的授权访问、序列类型和覆盖起点。

首轮当时的阻碍是网络白名单：插件和零售官网请求在代理 CONNECT 阶段返回 403，不是已确认的站点反爬或商品不存在。当时为调查曾保存以下自定义域名；列表仅供历史追溯，不是当前所需配置：

```text
arcteryx.com, outlet.arcteryx.com, regear.arcteryx.com,
rei.com, backcountry.com, altitude-sports.com, mec.ca,
distill.io, visualping.io, keepa.com, camelcamelcamel.com,
karmanow.com, joinhoney.com, shopsavvy.com, price.com,
chromewebstore.google.com
```

草稿保存成功不代表运行时已放行，也不代表已经发布。当前配置应以仅美国范围及实际所需存档来源为准，见 [第三方与历史说明](retailers-and-history.md)，不要据本历史清单重新添加加拿大商家域名。

[keepa-readme]: https://github.com/keepacom/api_backend/blob/683bfa839b84d172a156085bda0b29ce8c3eee45/README.md
[keepa-stats]: https://github.com/keepacom/api_backend/blob/683bfa839b84d172a156085bda0b29ce8c3eee45/src/main/java/com/keepa/api/backend/structs/Stats.java
[keepa-product]: https://github.com/keepacom/api_backend/blob/683bfa839b84d172a156085bda0b29ce8c3eee45/src/main/java/com/keepa/api/backend/structs/Product.java
[changedetection]: https://github.com/dgtlmoon/changedetection.io/blob/5305a34e31909d8d94488860b605773ba57c9f3f/README.md
[us-worker]: https://github.com/lethalgem/arcteryx-outlet-tracker/blob/1b8a4708223e1710c2dadd917ac7fc4413fba5a1/README.md
[us-inventory]: https://github.com/lethalgem/arcteryx-outlet-tracker/blob/1b8a4708223e1710c2dadd917ac7fc4413fba5a1/src/inventory.ts
[us-cron]: https://github.com/lethalgem/arcteryx-outlet-tracker/blob/1b8a4708223e1710c2dadd917ac7fc4413fba5a1/wrangler.toml
[ca-monitor]: https://github.com/TongTong0828/arcteryx-monitor/blob/3ef453ad25b2c3dcda0f124658d0ffa8bbbda53a/README.md
[ca-code]: https://github.com/TongTong0828/arcteryx-monitor/blob/3ef453ad25b2c3dcda0f124658d0ffa8bbbda53a/monitor.py
[stock-monitor]: https://github.com/kzhao5/arcteryx-stock-monitor/blob/a2a82309cc54a241367e8afd61cbdfb76946e2c7/README.md
[deals-schema]: https://github.com/wantai-dev/001-arcteryx-deals-platform/blob/b9d5cbbac1503dd4fef4722a40e8fa8f02f60c17/supabase_schema.sql
