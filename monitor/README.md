# BTC 实时信息监控

24 小时监控可能影响 BTC 的消息和盘面异常，分级推送，并记录每条消息之后 BTC 的涨跌，用于以后回测。

- 只用 Python 标准库（3.9 及以上），不需要 `pip install`
- 只抓公开、免费的来源，抓取频率较低（60 秒到 15 分钟一次）
- 只提供信息，不下单

## 监控内容

| 模块 | 内容 | 来源 |
|---|---|---|
| 新闻 | 加密媒体、中文快讯、官方机构、全网聚合（比特币 / 宏观 / 地缘） | CoinDesk、Cointelegraph、The Block、Decrypt、Bitcoin Magazine、Odaily；美联储、SEC、CFTC、币安公告；Google 新闻；GDELT |
| 盘面异常 | 5 分钟急涨急跌、成交量暴增、15 分钟持仓量剧变、资金费率极端 | 币安合约公开接口 |
| 跨市场 | 纳指期货、黄金、原油、美元指数、美元兑日元、美债 10 年收益率 | Yahoo Finance |
| 美股官方价 | QQQ、SPY、IWM、NVDA、MSTR、COIN、BMNR、CRCL、HOOD、MARA、IREN、BITO 的常规收盘、盘前盘后最后一笔、含夜盘的全时段最新价（面板的实时价直接连币安股票永续，不经过这里） | Yahoo Finance |
| 事件日历 | 美国高影响数据（CPI、非农、FOMC 等）、美股开盘；发布前 60 分钟和 10 分钟各提醒一次 | ForexFactory 日历 |
| 预期振幅 | 根据当前时段和过去 24h 活跃度，给出未来 1h 振幅中位数、4h 振幅 P80 | 一年回测得出（见 `../backtest/range_table.py`） |

## 分级

- 🔴 **红色，立即推送**：交易所出事、单笔 ≥ 1 亿美元的黑客事件、稳定币脱锚、入侵 / 导弹袭击 / 宣战、美联储意外动作或 FOMC 声明、监管起诉；BTC 5 分钟振幅 ≥ 1.2%；重大数据发布前 10 分钟。
- 🟡 **黄色，每 10 分钟汇总推送一次**：ETF 资金流、宏观数据、美联储讲话、关税和制裁、大机构买卖、持仓量异动、跨市场异动、数据发布前 60 分钟。
- ⚪ **灰色，只进状态页和日志**：其他相关新闻。

**降噪规则：**
- 没直接提到加密的聚合新闻（战争、宏观等），需要 2 家媒体报道才升为黄色，3 家才升为红色。
- 同一事件只有级别升高时才会再推一次。
- 启动后第一轮抓到的旧新闻只入库，不推送。

「偏多？/ 偏空？」只是按字面猜的，**不可靠**。等 `events.db` 积累几个月后，再回测哪类消息真的有用。

## 每条新闻的利多 / 利空判断（AI 标注）

关键词规则只能给出初步判断，大量新闻会落到「中性」。要让每条新闻都有可靠的判断，需要接入 AI，二选一：

**方式 1：Claude API（推荐，全自动）**

```bash
pip install -r requirements.txt
export ANTHROPIC_API_KEY=你的Key        # 在 console.anthropic.com 创建
export LABEL_MODEL=claude-opus-5-5      # 默认值；换成更便宜的模型请自行设置
```

每轮把最多 20 条（`LABEL_BATCH`）还没标注的新闻一次发给 Claude，拿回利多 / 利空 / 中性、强弱、一句中文理由和中文标题。红色和黄色新闻优先。Key 无效、被限流或断网时，自动退回关键词规则，不影响监控。

**方式 1b：DeepSeek API（不需要安装任何包）**

在本机运行时，把 Key 存进 macOS 钥匙串，`run.sh` 启动时自动读取，不写进任何文件：

```bash
security add-generic-password -s btc-dashboard-deepseek -a btc -w
```

（回车后粘贴 Key，输入时不显示。换 Key 时先 `security delete-generic-password -s btc-dashboard-deepseek` 再加。）在服务器上运行时改用环境变量：

```bash
export DEEPSEEK_API_KEY=你的Key          # 在 platform.deepseek.com 创建并充值
export DEEPSEEK_MODEL=deepseek-flash     # 默认值（DeepSeek V4.1 Flash）
```

设置了 `DEEPSEEK_API_KEY` 就自动使用 DeepSeek。它的 JSON 模式不强制格式，程序会逐条校验，漏掉或格式不对的下一轮重试。Key 无效或余额不足时自动退回关键词规则。

**方式 2：让 Muse 判断（不需要额外的 Key）**

```bash
export LABEL_MODE=muse
```

程序把待标注新闻按行写入 `out/label_queue.jsonl`。Muse 判断后，把结果按行追加到 `out/label_results.jsonl`，格式如下：

```json
{"key": "队列里的 key", "lean": "利多", "strength": "强", "reason": "一句中文理由", "title_zh": "中文标题"}
```

`lean` 只能是 利多 / 利空 / 中性，`strength` 只能是 强 / 中 / 弱。程序每分钟读取一次并合并。

AI 结果回来之前，面板显示关键词规则的结果，并标注「规则判断」；回来后改为「AI判断」。

## 模拟盘

`paper.py` 按仓库根目录 `rules_spec.json` 的统一规则，每根 5 分钟 K 线收盘后执行规则 A / B，逐笔记录到 `out/paper.json`（面板读取），状态保存在 `out/paper_state.json`，重启后接着跑。逻辑与 `../backtest/rules_backtest.py` 逐笔一致。需要 `pip install -r requirements.txt`（pandas、numpy），并且部署时要带上 `../backtest/` 里的 `zhang.py`、`direction_model.py`、`results/direction_weights_full.json`。

**模型喊单（自我验证）**：模拟盘每处理一根 5 分钟 K 线，都会检查面板「此刻结论」会不会给出参考信号（规则 A 或 B，不管模拟盘当时有没有持仓）。信号出现时，按面板给出的进场价、止损、止盈和持有时间记一条「模型·此刻结论」喊单，写到 `out/model_calls.json`；同一规则同方向 60 分钟内只记一次。面板打开时会合并这些记录，在「喊单记录与验证」里自动核对。面板开着时也会自己记（同一个 id，不会重复），所以监控程序没运行的时段只要面板开着也不会漏。

另外每分钟把价格、持仓量、资金费率、多空比追加到 `out/market_log.csv`，攒够数据后用来验证挤压雷达。

## 运行

```bash
python3 monitor.py --once --no-push   # 测试：跑一轮，不推送
./run.sh                              # 长期运行，崩溃后自动重启（日志在 out/monitor.log）；本机会自动用 ../backtest/.venv 的 Python
```

输出都在 `out/` 目录下：

- `status.html`：用浏览器打开就是实时状态页，每 30 秒刷新
- `status.json`：同样的内容，给其他程序读取
- `alerts.jsonl`：每条警报占一行
- `events.db`：SQLite 格式的事件日志，会自动回填每条消息发出后 1h / 4h 的 BTC 涨跌

## 推送设置（环境变量，都是可选）

数据发布到 GitHub（面板读取）：`GITHUB_REPO=logitlong/btc-dashboard`、`GITHUB_TOKEN=细粒度令牌`、`PUBLISH_EVERY=120`。

人工喊单的后台提醒：`RECORDS_REPO=logitlong/btc-records`（私有记录仓库，面板「连接云端」后喊单和交易日志存在这里）。设置后每分钟读一次喊单，持有中的单碰到止损 / 止盈、到时间或盘面突变时发 Mac 通知（`MAC_NOTIFY=0` 关闭）。令牌要同时能读写这两个仓库。


```bash
# 邮件（以 Gmail 应用专用密码为例；QQ 邮箱之前出现过退信）
export SMTP_HOST=smtp.gmail.com SMTP_PORT=465 SMTP_USER=你的邮箱 SMTP_PASS=应用专用密码 ALERT_TO=收件人1,收件人2
# Telegram（先找 @BotFather 建机器人）
export TG_TOKEN=机器人token TG_CHAT=你的chat_id
# 通用 Webhook（企业微信、飞书、Discord 等，收到的是 {"subject":..,"body":..}）
export WEBHOOK_URL=https://...
# 其他
export DISPLAY_TZ=Asia/Tokyo        # 显示时区，默认东京
export YELLOW_BATCH_SEC=600         # 黄色汇总间隔（秒）
export OUT_DIR=/path/to/out
```

## 交给 Muse 部署

完整步骤（创建 GitHub 令牌、选择新闻标注方式、发给 Muse 的指令、上线检查）见仓库根目录的 `DEPLOY_MUSE.md`。

## 已知限制

- **ETF 资金流**：Farside 网站拒绝程序访问，只能从新闻标题里捕捉。
- **GDELT**：频繁访问会被限流，所以每 15 分钟才抓一次，失败时自动降频。
- **X / Twitter 快讯、路透和彭博实时新闻**：需要付费 API，这一版没有接入。
- **速度**：免费来源通常比机构慢 1–15 分钟。盘面异常探测是兜底：先看到价格异动，再去查原因。
- **黑天鹅无法预测**：本程序只负责尽早发现，风控还要靠止损和仓位。
