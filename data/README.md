# 工作数据库

在仓库根目录运行：

```bash
PYTHONPATH=src python3 -m arcteryx_prices --db data/prices.sqlite3 init
```

`prices.sqlite3` 是本地 SQLite 工作库，不纳入 Git。当前产品仅监控美国 US / USD；旧加拿大关注停用且不在界面展示，已有记录保留供审计。新建环境执行 `init` 会创建空库，不能据文件结构恢复真实历史。请勿导入 `examples/observations.example.json` 中的合成价格。表结构在 `db/`，研究及实际验证证据在 `research/`。
