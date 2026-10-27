# 在自己的电脑上检查并发送 Gmail 提醒

单次命令会读取已到期关注、保存价格、发送符合规则的邮件，然后退出。系统定时任务负责再次调用它，因此无需一直开着网页。电脑关机、休眠或断网期间不会正常监控；再次运行只读取当时的价格，不会补出停机期间的行情。

## 1. 添加自己的关注

运行网页工作台，粘贴美国官网、美国 Outlet 或 REI 的商品链接，读取变体后选择颜色、尺码和目标价或其他提醒规则。网页中的目标价按美元输入，例如 USD 200 填 `200`，无需手写 SKU 或 JSON。也可使用 `discover` 与 `watch-add`，见 [README](../README.md#命令行)。使用同一个数据库路径。

```bash
python3 scripts/run-monitor.py --db data/prices.sqlite3 serve --no-worker
```

Windows 可将 `python3` 改为 `py -3 -X utf8`。`--no-worker` 让网页只负责管理关注，检查交给单次命令。默认检查间隔是 60 分钟；需要更快时，在新增关注或“设置提醒”的频率选项中选择 15 分钟。商品尚未选定时可以先完成试运行或邮件配置；仓库中的 Beta 示例和工作库试点都不代表你的购买需求。

男款 Gamma MX 与 Sidewinder 的 XL / XXL 已另备[批量导入清单与来源](gamma-mx-sidewinder.md)，可一次加入 20 个关注，预设为每 15 分钟、已观测新低提醒。导入不会自动停用已有的示例或其他关注。

## 2. 配置 Gmail

在 Google 账号开启两步验证后，创建一个仅供此程序使用的 [应用专用密码](https://support.google.com/accounts/answer/185833)。部分组织账号或高级保护账号不提供此功能；这种情况下当前 SMTP 方式不可用。不要填写 Google 账号的日常登录密码，也不要把应用专用密码发到聊天或提交到 Git。

复制 [配置示例](../examples/gmail.env.example) 到仓库之外的私人目录，填写：

```dotenv
ARC_GMAIL_USER=you@gmail.com
ARC_GMAIL_APP_PASSWORD=your-google-app-password
ARC_ALERT_TO=you@gmail.com
```

`ARC_ALERT_TO` 只能填一个邮箱，省略时发给自己。文件采用 `KEY=value`，不运行 shell 命令、不展开变量；同名环境变量优先于文件。macOS/Linux 上将文件权限设为 `600`，程序会拒绝其他用户可读写的配置文件：

```bash
chmod 600 /absolute/private/gmail.env
```

程序通过 `smtp.gmail.com:465` 使用 TLS 发送，不关闭证书校验。运行电脑需要能访问该 SMTP 端口；仅允许 HTTP 代理的受限环境可能无法发送。密码不写入数据库或日志。

配置完成后，可主动运行一次测试命令。它会发送一封明确标为配置测试的真实邮件，不读取商品或修改价格数据库：

```bash
python3 scripts/run-monitor.py gmail-test --env-file /absolute/private/gmail.env
```

命令成功只表示 Gmail 接受发送，仍需查看收件箱或垃圾邮件文件夹确认收到。本次开发验证没有使用你的账号实际发信。

## 3. 先预览，再正常运行

在仓库根目录使用 Python 3.10+；无需安装第三方包：

```bash
# 无需 Gmail 账号或密码；真实采集并保存价格，但只预览邮件。
python3 scripts/run-monitor.py --db data/prices.sqlite3 run-once --dry-run

# 检查到期关注，并发送尚未送达的有效提醒。
python3 scripts/run-monitor.py --db data/prices.sqlite3 run-once --notify gmail --env-file /absolute/private/gmail.env
```

Windows 可将 `python3` 改为 `py -3 -X utf8`，并使用自己的 Windows 路径。`scripts/run-monitor.py` 与模块命令行为相同，不需要设置 `PYTHONPATH`。

试运行依然会保存真实价格、检查状态和站内提醒，便于积累历史及保留商家要求的等待期限；它只关闭邮件发送和通知记录写入。未配置收件人时，预览不代表“某个邮箱尚未收到”的清单。需要针对已配置收件人查看去重后的预览时，可显式加 `--env-file /absolute/private/gmail.env`，预览仍不要求应用专用密码。

`--force` 可检查全部启用的关注，仍遵守商家的等待期限。没有符合条件的提醒就不发测试邮件；首次采集只建立价格基线，`new_low` 需要之后出现更低价格才触发。目标价规则首次达标可以提醒。

价格邮件来自已有的两次核验结果，仍启用的美国关注提醒可在核验后 7 天内补发，包含具体颜色、尺码、USD 价格、原始核验时间和商品链接。邮件中的价格不含税运，收到时优惠或库存可能已变化，延迟收到的邮件不代表当前仍可购买。超过 7 天计为过期并在命令汇总中显示；旧版因 24 小时限制过期、但仍在 7 天窗口内的未发送提醒会重新尝试。

一般采集故障连续失败三次后发送一次提示；来源拦截则首次发现就生成一封来源通知，覆盖同一主机所有已启用关注。有效采集恢复后，再次发生故障可以重新提醒。发送失败会保存状态，后续运行按 30 分钟起、最多 6 小时的递增间隔重试。成功发送的事件不因程序重启而重复发送；若 Gmail 已接受邮件、程序却在落库前崩溃，仍可能重发一次。固定的 Message-ID 有助于识别这种重复，但无法保证邮箱端去重。

来源拦截邮件标题和正文注明“被网站拦截”，列出受影响关注数和下次计划检查时间。HTTP 403 表示访问被拒绝，不能仅凭它认定网站使用了哪种反机器人规则；也应核对网络和代理策略。首次拦截暂停同一主机的请求 1 小时，持续拦截逐步延长至最多 24 小时。同一次故障只通知一次，不发送原始拦截页面或敏感响应内容。`--dry-run` 始终只预览，即使首次被拦也不发送；只有配置好 Gmail 的正式运行才尝试投递。

监控邮件由 `run-once --notify gmail` 发送；`gmail-test` 仅在你主动调用时发送配置测试邮件。`serve`、`worker` 与 `check` 不会自行启用外部通知。发送逻辑的离线测试使用模拟 SMTP，不表示已经通过你的 Gmail 实际投递。

命令输出 JSON 汇总；退出码 `0` 表示正常完成或已有另一轮正在执行，`1` 表示采集或邮件仍有失败、待重试，或尚未添加启用的关注，`2` 表示配置错误。定时任务应保留输出和错误日志，便于发现 Gmail 本身不可用的情况。

## 4. 让电脑每 5 分钟调用一次

检查间隔与调度间隔是两件事：把具体关注设为 15 分钟，再让系统每 5 分钟运行一次命令。程序只抓取已到期的关注；其余轮次可以处理邮件重试，默认仍为 60 分钟的关注不会因此每 5 分钟抓取。采集完成时间与系统调度可能错开，正常运行时实际检查通常约隔 15–20 分钟；限流、较慢的响应或停机还会推迟。

15 分钟间隔的理论初次读取次数是每小时 4 次，约为 60 分钟间隔的 4 倍；提醒复核会额外读取，同页变体可共用初次读取。可只加快最关心的几件商品。更改间隔不能绕过限流或验证拦截后的等待。不要给定时任务加 `--force`。

所有路径使用绝对路径，任务中只放凭据文件路径，不放密码；以下配置由你在自己的电脑上安装。没有持续运行电脑的情况下，修改频率也无法补回离线时的行情。

定时模板把数据库放在仓库之外的私人目录。首次导入、网页服务与定时任务必须使用同一个 `--db` 路径；已经有数据时，先停止相关进程并备份，再移动数据库及其 SQLite 旁文件，避免因路径不一致启动一个空库。README 的仓库内路径仍可用于快速体验，程序支持任意可写路径。

**macOS：**复制 [launchd 模板](../examples/schedulers/com.arcteryx.price-monitor.plist) 到 `~/Library/LaunchAgents/com.arcteryx.price-monitor.plist`。替换模板中的 Python、仓库、数据库、凭据与日志路径；终端执行 `command -v python3` 可找到 Python。先创建私人配置目录，再执行：

```bash
plutil -lint ~/Library/LaunchAgents/com.arcteryx.price-monitor.plist
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.arcteryx.price-monitor.plist
```

这是登录后的用户任务，每 300 秒运行一次；休眠时不采集，唤醒后是否立即触发由系统安排。卸载用 `launchctl bootout gui/$(id -u) ~/Library/LaunchAgents/com.arcteryx.price-monitor.plist`。模板把日志覆盖写到 `last-run.log`，只保留最近一轮。

**Windows：**复制 [PowerShell 模板](../examples/schedulers/run-monitor.ps1.example) 到私人目录，改名为 `run-monitor.ps1` 并替换其中的绝对路径。凭据文件放在仅自己的 Windows 账号可访问的目录。先手动运行脚本，再在“任务计划程序”中创建任务：

- 选择“仅当用户登录时运行”；触发器为每天、每 5 分钟重复、持续时间为无限期。
- 程序填 `powershell.exe`，参数填 `-NoProfile -NonInteractive -File "C:\Private\run-monitor.ps1"`，改成实际路径。
- 启用“错过计划开始时间后尽快启动”；若希望用电池时也检查，关闭仅交流电运行及切换电池时停止的条件。
- 已在运行时选择“不启动新实例”。任务历史中的退出码和模板覆盖写入的 `last-run.log` 可用于排查失败。

不要启用强制唤醒电脑。脚本若被本机执行策略阻止，应按该电脑的策略处理；模板没有自动放宽策略。模板使用 Python UTF-8 模式，避免中文商品名在重定向日志时导致编码错误。macOS 和 Windows 的任务安装均需在目标电脑验证，当前 Linux 开发环境只校验了模板结构，没有运行这两种系统的调度器。

**Linux：**原说明中的 cron 命令也已独立保存为 [arcteryx.cron](../examples/schedulers/arcteryx.cron)。使用 `crontab -e` 把下行加入当前用户的任务，替换全部绝对路径，并确认日志目录已存在；无需新增常驻网页进程：

```cron
*/5 * * * * umask 077; "/absolute/python3" "/absolute/arcteryx/scripts/run-monitor.py" --db "/absolute/private/arcteryx/prices.sqlite3" run-once --notify gmail --env-file "/absolute/private/gmail.env" >> "/absolute/private/monitor.log" 2>&1
```

Linux 模板追加日志，保留前几轮报错；可配合 [logrotate 示例](../examples/schedulers/arcteryx.logrotate)按天轮转、保留 7 份压缩旧日志。它需要目标系统已有 logrotate 并配置日常执行；这里没有自动安装。macOS 和 Windows 示例仍只保留最近一次日志。

三个方案都使用系统已有调度功能，没有另外运行常驻网页服务。电脑恢复在线后读取的新价格不能代表离线期间价格；如果整个任务没有启动，程序也无法给自己发送故障邮件。
