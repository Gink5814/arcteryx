# Therme Down Jacket：待核验关注

用户提供吊牌照片并要求关注这件衣服。目前仅登记购买意向，**尚未加入可执行监控，也不会产生自动提醒**；商品 URL 和网页变体 SKU 尚未核验。结构化记录见 [关注请求与查询证据](../research/therme-watch-request.json)。

## 已从照片确认

- 商品：Arc’teryx **Therme Down Jacket — Men’s**，不能用 Therme Parka 或其他版本代替。
- 颜色：**Black Sapphire**。
- 照片尺码：**XL / Extra Large**；先按照片这一组合关注。
- 零售贴纸编号：`C11-812-0004`。这是照片上的编号，尚不能当作网上可订购 SKU。
- 原吊牌可见编号：`001280`，位于颜色下方；不是已核实的完整款号。
- 标签标价 **USD 649.73**，标注 Outlet；标签原价与原吊牌 MSRP 均为 **USD 800.00**。

以上金额仅来自用户提供的照片，照片拍摄日期、具体商家和门店未知。记录于 **2026-10-29**；不代表当日网价、现货或历史最低价，不导入价格历史。照片未明确显示 REI 商标，不能仅凭编号格式认定商家。

## 提醒偏好

沿用用户此前选择：美国、美元、`available_or_target`、`target_price: null`。核实商品后，首次确认有货即可提醒，之后补货再提醒；不把照片价格自动设为预算。正常检查间隔 15 分钟，遵守来源退避和二次确认。

## 本次核验结果与接入方式

**2026-10-29 16:14:46（America/Denver，UTC−06:00）**，尝试用照片编号作为线索[搜索 REI](https://www.rei.com/search?q=C11812+Therme+Down+Jacket)，环境代理返回 `Tunnel connection failed: 403 Forbidden`。请求未取得商家响应，不能据此判断 REI 有货、缺货或拦截了用户。

后续应从可访问来源取得准确商品链接，并核对型号、Black Sapphire、XL 和真实 SKU。官网及 Outlet 可先运行：

```powershell
py -3 -X utf8 scripts/run-monitor.py discover --search "therme"
```

列表检索不保证覆盖所有页面；只选择与照片相同的 Jacket。取得并核实美国商品页后，在实际监控电脑执行（将占位符替换为真实 URL）：

```powershell
py -3 -X utf8 scripts/run-monitor.py --db data/prices.sqlite3 watch-discover "<已核实的美国商品URL>" --sizes XL --colors "Black Sapphire" --rule available_or_target
```

仅导入实际匹配到的变体；页面没有该组合时保持待核验，不猜 SKU、不自动换颜色或换成 Parka。之后仍需在实际运行电脑完成 Gmail 和定时任务配置，才会持续检查并通知。
