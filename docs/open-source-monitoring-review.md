# 更成熟的开源价格监控：源码比较与本轮优化

调研日期：2026-10-27 UTC。目标仅限美国商品、USD，以及明确的款式、颜色、尺码和库存。本轮读取维护者仓库、固定提交的实现、许可证和相关测试；没有安装或运行这些第三方应用，也没有取得它们追踪前的零售价格历史。

## 选择结论

**PriceBuddy 最值得参考完整价格监控产品的设计，changedetection.io 最值得评估作为未来的动态网页采集服务，webchanges 对本轮采集稳定性改进最有直接价值。** 当前保留已验证的美国官网、Outlet、REI 变体采集器，并独立实现适合本项目的调度和页面复用。

| 项目 | 源码验证到的优势 | 对本项目的限制 | 采用方式 |
| --- | --- | --- | --- |
| [PriceBuddy](https://github.com/jez500/pricebuddy) | 专用价格应用；价格历史、商家提取规则、HTTP/浏览器采集、延迟重试、失败通知抑制 | 通用提取字段不要求 SKU/颜色/尺码；美国预设只见 Amazon/eBay，未验证 Arc'teryx/REI；PHP 8.4、Laravel 12、MySQL、浏览器服务增加运行成本 | 参考持久化重试、暂停后停止重试、复核不使用缓存的设计 |
| [changedetection.io](https://github.com/dgtlmoon/changedetection.io) | 页面监控服务、Playwright、Browser Steps、API、通知及变化记录 | 所查默认价格提取会递归取首个价格/库存，不能证明它属于目标尺码；没有本轮商家实测 | 保留为动态页面采集候选，取得页面后仍需要精确变体核验 |
| [webchanges](https://github.com/mborsetti/webchanges) | 临时 HTTP 错误分类、Retry-After、指数退避、同站点请求间隔、SQLite 快照 | 所查实现包含阻塞等待，并将 Retry-After 截断到 60 秒；快照不是现成的变体价格数据库 | 参考错误分类；本项目用持久化截止时间调度，并将单次等待上限设为 24 小时 |
| [urlwatch](https://github.com/thp/urlwatch) | 轻量命令行页面监控；错误、恢复、304 和变化状态分开；保存快照 | `max_tries` 是连续失败报告抑制，不是立即重试次数；页面差异不等于价格变化 | 参考失败/恢复状态与历史保留方式 |
| [Huginn](https://github.com/huginn/huginn) | 通用自动化；数据库任务唯一性、执行锁、变化事件去重 | 需要额外的 Ruby/数据库运行栈；没有现成的严格变体价格模型 | 参考并发与去重原则，保留本项目已有租约和提醒去重 |

星数和近期提交仅作为项目状态记录，未用于证明采集准确性。上述项目均未在本轮实测美国始祖鸟商品兼容性，也不能自动补齐开始监控之前的 90/365 天价格。

## 具体源码依据

- PriceBuddy 的 [RetryUrlPriceJob](https://github.com/jez500/pricebuddy/blob/f606128f5f723b70fe817362954e251c02143745/app/Jobs/RetryUrlPriceJob.php) 使用延迟任务，恢复、暂停或删除后停止；[ScrapeUrl](https://github.com/jez500/pricebuddy/blob/f606128f5f723b70fe817362954e251c02143745/app/Services/ScrapeUrl.php) 的重试会关闭页面缓存。[美国商家预设](https://github.com/jez500/pricebuddy/blob/f606128f5f723b70fe817362954e251c02143745/database/seeders/Stores/usa.php) 不构成 Arc'teryx 或 REI 已支持的证据。
- webchanges 的 [HTTP 实现](https://github.com/mborsetti/webchanges/blob/c2d7e5fae2e5af8380ddf3ea0c1772dc444a72cf/webchanges/jobs/_url.py) 和 [worker](https://github.com/mborsetti/webchanges/blob/c2d7e5fae2e5af8380ddf3ea0c1772dc444a72cf/webchanges/worker.py) 支持错误分类及等待；其具体等待上限与本项目的服务器截止时间策略不同。
- changedetection.io 的 [价格提取](https://github.com/dgtlmoon/changedetection.io/blob/5305a34e31909d8d94488860b605773ba57c9f3f/changedetectionio/processors/restock_diff/pure_python_extractor.py) 与 [Playwright 采集器](https://github.com/dgtlmoon/changedetection.io/blob/5305a34e31909d8d94488860b605773ba57c9f3f/changedetectionio/content_fetchers/playwright.py) 说明浏览器能力和价格身份核验是两件不同的事。
- urlwatch 的 [worker](https://github.com/thp/urlwatch/blob/645796df19f1a7a654e9e2c6b0f4cce181d88483/lib/urlwatch/worker.py) 区分错误和变化；Huginn 的固定提交与具体任务/去重文件见下面的来源清单。

完整的固定提交、路径、抓取时间、内容哈希、许可证和逐项判断位于 [价格应用来源清单](../research/oss-price-projects.json) 与 [监控引擎来源清单](../research/oss-monitor-projects.json)。PriceBuddy 标明 GPL-3.0-only，changedetection.io 为 Apache-2.0，urlwatch 为 BSD 风格条款，webchanges 包含 MIT 与保留的 BSD 条款，Huginn 为 MIT。本轮仅参考公开设计，未复制第三方实现，应用仍使用 Python 标准库与 SQLite。

## 本轮落实到项目中的改进

### 限流与失败调度

采集器保留 HTTP 状态、是否可重试以及 Retry-After 的明确截止时间。秒数和 HTTP 日期都可识别；无效值采用本地退避。调度器将商家的等待截止时间写入 SQLite，进程重启后仍有效；自动检查、立即检查及商品变体发现共享限制。

网络错误及可重试 HTTP 错误采用有上限的指数退避，成功后恢复正常间隔。身份或页面解析错误不会触发密集的网络重试。服务器 Retry-After 在 24 小时内遵守其等待要求；超过该上限的秒数或日期会截断为 24 小时，避免异常值长期冻结来源。等待通过下次调度时间表达，不阻塞整个 worker；另一个主机的商品可以继续检查。已经在途的其他请求不保证能被取消，本版也不实现每秒请求数配额。

### 同页变体共用初次读取，提醒复核独立读取

同一轮调度中，相同规范化商品 URL 的初次读取只保留一份页面、原始采集时间及哈希，然后分别核对各 SKU 的颜色、尺码、价格与库存。缓存最多保留 8 个有大小上限的响应，按 LRU 淘汰，且不跨调度轮次。

提醒候选仍重新请求页面，不使用初次读取或其他候选的复核结果。这样减少多尺码关注的重复下载，同时保留价格/库存二次确认。原始采集时间不会因页面复用而变成较新的价格时间。

### 用户能看见等待和恢复

卡片显示连续失败次数和下次可检查时间；等待期间禁用立即检查，但设置和暂停仍可操作。失败或等待不会生成零价、重置促销去重状态或补造历史；已有真实历史继续保留。前端在截止时间过去后重新计算按钮状态，不依赖服务器改写原始字段。

## 验证范围

离线测试使用隔离的合成页面和数据库，验证同页不同尺码只初采一次、价格/库存仍隔离、提醒独立复核、下一轮重新读取、Retry-After 秒数/日期与异常输入、重启后限流有效、其他主机继续执行、失败恢复以及前端到期状态。

本轮实际执行 **107 项 Python 测试**与浏览器回归，均通过。2026-10-27 UTC 对工作库中 5 个美国关注执行一轮真实采集：它们对应 4 个商品页，共读取页面 4 次，5 个关注均检查成功。相对于逐关注各读取一次，初次页面请求从 5 次减少到 4 次（该轮减少 20%）；本轮没有提醒候选，所以没有额外复核请求。独立复核由合成场景测试证明，不能将这次正常采集当成真实限流恢复或长期稳定性验证。此前 21 条观测及 12 条历史参考完整保留。结果见 [真实采集验证](../research/resilience-live-validation.json)。

真实价格仍由既有美国采集器取得。压力/限流场景通过合成响应测试，不向真实商家制造大量请求。没有新增浏览器运行服务、外部通知发送或来源访问绕过；这些不是本轮验证结果。
