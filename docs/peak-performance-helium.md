# Peak Performance Helium AOP：待接入关注

用户提供吊牌照片并要求关注这件衣服。**目前已登记购买意向，尚未启用自动监控或提醒。** 具体美国商品页、网页变体与品牌采集适配均待核验；结构化线索见 [关注请求记录](../research/peak-performance-helium-watch-request.json)。

## 照片信息

- 品牌：**Peak Performance**。
- 标签商品名：**Helium AOP Down Hood Jack**，按连帽羽绒夹克查找；标签使用缩写，完整商品名和版本待网页核实。
- 款号：**G80861020**；条码：**7333529014221**。
- 性别与尺码：**Women，S**。本件不沿用男款 XL / XXL 的偏好。
- 颜色标签原文：**331 SNOWBAN**。保留原文，完整名称及配色待核实，不自行补全。
- 标签金额：**$290.00**，照片未明确写 USD；不能仅凭美元符号确认币种。

照片拍摄时间、商家和门店未知。上述线索记录于 **2026-10-29**，金额不是实时网价，不导入价格历史，也不自动设为目标价。

## 关注设置与接入条件

搜索范围仍为美国站、美元报价。意向为同款、同色、女款 S，使用 `available_or_target`、`target_price: null`，首次确认有货和后续补货时提醒，正常检查间隔 15 分钟。

**2026-10-29 16:20:30（America/Denver，UTC−06:00）**，尝试读取吊牌上的 [Peak Performance 官网](https://www.peakperformance.com/) 以寻找美国商品页，环境代理返回 `Tunnel connection failed: 403 Forbidden`。未取得商家响应，不能据此判断商品是否在售。

现有采集器仅支持 Arc’teryx 官网、Outlet 与 REI 的 Arc’teryx 商品；`discover --search` 也仅检索 Arc’teryx 男装列表，不能用于本件。网络恢复后，仍须取得真实商品页并适配相应来源，核实美国配送、USD、女款、款号、颜色、S 码和明确库存，再导入具体网页变体并测试提醒。不得只扩大域名白名单或把条码冒充网页 SKU 来声称已接入。

该请求不替换已有 Gamma MX、Sidewinder 或 Therme 关注。照片中的其他国家印刷文字不改变用户只关注美国来源的要求。
