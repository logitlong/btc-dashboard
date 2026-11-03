"""BTC 实时信息监控：新闻 + 盘面异常 + 经济日历 + 推送 + 事件日志。

只用 Python 标准库（3.9+），可直接放在任何 Linux / macOS 服务器上长期运行。

  python3 monitor.py              # 持续运行
  python3 monitor.py --once       # 跑一轮就退出（测试用）
  python3 monitor.py --minutes 10 # 跑 10 分钟后退出

推送（都可选，用环境变量开启，见 README）：邮件 SMTP / Telegram / 通用 Webhook。
输出文件（都在 OUT_DIR，默认 ./out）：
  alerts.jsonl  每条推送一行 JSON，方便 Muse 或其他程序读取
  status.json   当前状态（价格、预期振幅、下一个事件、最近警报）
  status.html   可直接用浏览器打开的状态页（每 30 秒刷新）
  events.db     SQLite 事件日志，自动回填新闻发出后 1h / 4h 的 BTC 涨跌，用于以后回测
"""
import argparse
import email.utils
import hashlib
import html
import json
import os
import re
import smtplib
import sqlite3
import statistics
import sys
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.mime.text import MIMEText
from zoneinfo import ZoneInfo

import labeler as LB
import rules
import market_data as MD
import signal_logic as SL
import sources as S

OUT = os.environ.get("OUT_DIR", os.path.join(os.path.dirname(os.path.abspath(__file__)), "out"))
TZ = ZoneInfo(os.environ.get("DISPLAY_TZ", "Asia/Tokyo"))
NY = ZoneInfo("America/New_York")
UA = {"User-Agent": "Mozilla/5.0 (compatible; btc-monitor/1.0; personal use)"}
YELLOW_BATCH_SEC = int(os.environ.get("YELLOW_BATCH_SEC", "600"))
MAX_AGE_H = 6  # 超过 6 小时的旧新闻不推送

# 预期振幅尺子（回测得出：过去 24h 振幅三分位 2.44% / 3.77%；单位 %）
RULER = {
    "周末": [(0.28, 1.14), (0.42, 1.51), (0.65, 2.22)],
    "工作日非美股时段": [(0.37, 1.30), (0.46, 1.59), (0.61, 2.17)],
    "美股交易时段": [(0.55, 1.77), (0.71, 2.02), (0.92, 2.54)],
    "美股开盘那1小时": [(0.83, 2.49), (0.96, 2.68), (1.28, 3.77)],
}


# ---------------- 工具 ----------------

def now_utc():
    return datetime.now(timezone.utc)


def fmt(dt):
    return dt.astimezone(TZ).strftime("%m-%d %H:%M")


def log(*a):
    print(datetime.now(TZ).strftime("%H:%M:%S"), *a, flush=True)


def get(url, timeout=20):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def get_json(url, timeout=20):
    return json.loads(get(url, timeout))


def strip_html(s):
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", s or ""))).strip()


def parse_date(s):
    if not s:
        return None
    try:
        return email.utils.parsedate_to_datetime(s).astimezone(timezone.utc)
    except Exception:
        pass
    try:
        d = datetime.fromisoformat(s.strip().replace("Z", "+00:00"))
        return (d if d.tzinfo else d.replace(tzinfo=timezone.utc)).astimezone(timezone.utc)
    except Exception:
        pass
    try:  # GDELT：20261021T183000Z
        return datetime.strptime(s.strip(), "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
    except Exception:
        return None


# ---------------- 存储 ----------------

class Store:
    def __init__(self, path):
        self.db = sqlite3.connect(path)
        self.db.execute("""CREATE TABLE IF NOT EXISTS items(
            key TEXT PRIMARY KEY, first_seen TEXT, published TEXT, source TEXT, title TEXT, link TEXT,
            level TEXT, relevance TEXT, lean TEXT, cluster TEXT, pushed INTEGER DEFAULT 0,
            price_at REAL, ret_1h REAL, ret_4h REAL)""")
        self.db.execute("CREATE TABLE IF NOT EXISTS flags(key TEXT PRIMARY KEY, at TEXT)")
        self.db.commit()

    def seen(self, key):
        return self.db.execute("SELECT 1 FROM items WHERE key=?", (key,)).fetchone() is not None

    def add(self, row):
        self.db.execute("INSERT OR IGNORE INTO items(key,first_seen,published,source,title,link,level,relevance,lean,cluster,pushed,price_at)"
                        " VALUES(:key,:first_seen,:published,:source,:title,:link,:level,:relevance,:lean,:cluster,:pushed,:price_at)", row)
        self.db.commit()

    def flag(self, key):
        if self.db.execute("SELECT 1 FROM flags WHERE key=?", (key,)).fetchone():
            return False
        self.db.execute("INSERT INTO flags VALUES(?,?)", (key, now_utc().isoformat()))
        self.db.commit()
        return True


# ---------------- 推送 ----------------

class Notifier:
    def __init__(self, push=True):
        self.push = push
        self.queue = []
        self.last_flush = time.time()
        self.recent = []  # 给状态页

    def send(self, level, title, body, kind):
        email = SL.is_signal_email(kind, level)
        subject = f"[BTC信号] {title}" if email else title
        rec = dict(time=now_utc().isoformat(), time_local=fmt(now_utc()), level=level, kind=kind, title=title, body=body,
                   email=email, subject=subject)
        self.recent.insert(0, rec)
        self.recent = self.recent[:60]
        with open(os.path.join(OUT, "alerts.jsonl"), "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        icon = {"red": "🔴", "yellow": "🟡", "gray": "⚪"}[level]
        log(icon, f"[{kind}]", title)
        if getattr(self, "outbox", None) and (level in ("red", "yellow") or kind in ("背景", "共振")):
            self.outbox.add(level, kind, title, body, email=email, subject=subject)
        # 邮件只发规格里的三类（共振档位升高、盘面异动、仓位异动），而且立刻发，不进黄色汇总。
        # 新闻、日历、跨市场、背景、档位回落只进记录。GitHub Actions 用 --no-push，这里也不会真的发出去。
        if email:
            self._deliver(subject, body)

    def flush(self, force=False):
        if self.queue and (force or time.time() - self.last_flush >= YELLOW_BATCH_SEC):
            self._deliver(f"🟡 BTC 监控：{len(self.queue)} 条黄色提醒", "\n\n".join(self.queue))
            self.queue = []
            self.last_flush = time.time()

    def _deliver(self, subject, body):
        if not self.push:
            return
        e = os.environ
        if e.get("SMTP_HOST") and e.get("ALERT_TO"):
            try:
                msg = MIMEText(body, "plain", "utf-8")
                msg["Subject"], msg["From"], msg["To"] = subject, e.get("SMTP_FROM", e.get("SMTP_USER", "")), e["ALERT_TO"]
                with smtplib.SMTP_SSL(e["SMTP_HOST"], int(e.get("SMTP_PORT", "465")), timeout=20) as s:
                    if e.get("SMTP_USER"):
                        s.login(e["SMTP_USER"], e.get("SMTP_PASS", ""))
                    s.sendmail(msg["From"], [a.strip() for a in e["ALERT_TO"].split(",")], msg.as_string())
            except Exception as ex:
                log("邮件发送失败:", ex)
        if e.get("TG_TOKEN") and e.get("TG_CHAT"):
            try:
                data = urllib.parse.urlencode({"chat_id": e["TG_CHAT"], "text": f"{subject}\n\n{body}"[:4000]}).encode()
                urllib.request.urlopen(f"https://api.telegram.org/bot{e['TG_TOKEN']}/sendMessage", data=data, timeout=20)
            except Exception as ex:
                log("Telegram 发送失败:", ex)
        if e.get("WEBHOOK_URL"):
            try:
                req = urllib.request.Request(e["WEBHOOK_URL"], data=json.dumps({"subject": subject, "body": body}, ensure_ascii=False).encode(),
                                             headers={"Content-Type": "application/json"})
                urllib.request.urlopen(req, timeout=20)
            except Exception as ex:
                log("Webhook 发送失败:", ex)


# ---------------- 新闻 ----------------

def parse_feed(src, raw):
    out = []
    if src["kind"] == "binance":
        for cat in json.loads(raw).get("data", {}).get("catalogs", []):
            for a in cat.get("articles", []):
                pub = datetime.fromtimestamp(a["releaseDate"] / 1000, timezone.utc) if a.get("releaseDate") else None
                out.append(dict(title=a["title"], link=f"https://www.binance.com/en/support/announcement/{a.get('code','')}",
                                summary=cat.get("catalogName", ""), published=pub))
        return out
    if src["kind"] == "gdelt":
        for a in json.loads(raw).get("articles", []):
            out.append(dict(title=a.get("title", ""), link=a.get("url", ""), summary=a.get("domain", ""), published=parse_date(a.get("seendate"))))
        return out
    root = ET.fromstring(raw)
    for it in root.iter():
        tag = it.tag.split("}")[-1]
        if tag not in ("item", "entry"):
            continue
        g = {c.tag.split("}")[-1]: c for c in it}
        title = strip_html(g["title"].text if "title" in g else "")
        link = ""
        if "link" in g:
            link = g["link"].text or g["link"].attrib.get("href", "")
        summ = strip_html((g.get("description") or g.get("summary") or g.get("encoded") or ET.Element("x")).text or "")[:400]
        pub = parse_date((g.get("pubDate") or g.get("published") or g.get("updated") or ET.Element("x")).text)
        out.append(dict(title=title, link=(link or "").strip(), summary=summ, published=pub))
    return out


class News:
    def __init__(self, store, notifier, price_fn):
        self.store, self.n, self.price_fn = store, notifier, price_fn
        self.next_at = {s["name"]: 0 for s in S.NEWS_SOURCES}
        self.fail = {s["name"]: 0 for s in S.NEWS_SOURCES}
        self.clusters = []  # (time, tokens, cluster_id, set(sources), pushed_level)
        self.first_pass = {s["name"]: True for s in S.NEWS_SOURCES}

    def cluster_of(self, title, source_name):
        tk = rules.tokens(title)
        cutoff = now_utc() - timedelta(hours=6)
        self.clusters = [c for c in self.clusters if c[0] > cutoff]
        for c in self.clusters:
            if rules.similar(tk, c[1]) >= 0.5:
                c[3].add(source_name)
                return c
        c = [now_utc(), tk, hashlib.md5(title.encode()).hexdigest()[:10], {source_name}, None]
        self.clusters.append(c)
        return c

    def poll(self):
        for src in S.NEWS_SOURCES:
            if time.time() < self.next_at[src["name"]]:
                continue
            try:
                items = parse_feed(src, get(src["url"]))
                self.fail[src["name"]] = 0
                self.next_at[src["name"]] = time.time() + src["every"]
            except Exception as ex:
                self.fail[src["name"]] += 1
                backoff = min(src["every"] * 2 ** self.fail[src["name"]], 3600)
                self.next_at[src["name"]] = time.time() + backoff
                log(f"抓取失败 {src['name']}: {ex}（{int(backoff)} 秒后重试）")
                continue
            seed = self.first_pass[src["name"]]
            self.first_pass[src["name"]] = False
            for it in items:
                self.handle(src, it, seed)

    def handle(self, src, it, seed):
        if not it["title"]:
            return
        key = hashlib.sha1((it["link"] or it["title"]).encode()).hexdigest()
        if self.store.seen(key):
            return
        cls = rules.classify(it["title"], it["summary"], src)
        pub = it["published"] or now_utc()
        stale = (now_utc() - pub) > timedelta(hours=MAX_AGE_H)
        if cls is None:
            self.store.add(dict(key=key, first_seen=now_utc().isoformat(), published=pub.isoformat(), source=src["name"], title=it["title"],
                                link=it["link"], level="none", relevance="", lean="", cluster="", pushed=0, price_at=None))
            return
        # 用具体媒体名做「多源印证」：Google 新闻取标题末尾「 - 媒体名」，GDELT 取域名
        outlet = src["name"]
        m = re.search(r"\s-\s([^-]{2,40})$", it["title"])
        if src["kind"] == "gdelt":
            outlet = it["summary"] or outlet
        elif src["name"].startswith("Google") and m:
            outlet = m.group(1).strip()
        c = self.cluster_of(it["title"], outlet)
        n_src = len(c[3])
        level = cls["level"]
        if cls["relevance"] == "间接" and src["cred"] < 2:
            # 没提到加密的聚合新闻（战争、宏观）量很大：2 家媒体报道才算黄，3 家才算红
            level = ("red" if level == "red" and n_src >= 3 else
                     "yellow" if level in ("red", "yellow") and n_src >= 2 else "gray")
        elif level == "gray" and n_src >= 3:
            level = "yellow"
        corroborated = n_src >= 2
        # 同一事件只在级别升高时推送；首轮入库的事件只记录级别不推送
        higher = c[4] is None or rules.ORDER[level] > rules.ORDER[c[4]]
        push = not seed and not stale and level in ("red", "yellow") and higher
        if seed and higher:
            c[4] = level
        price = self.price_fn()
        self.store.add(dict(key=key, first_seen=now_utc().isoformat(), published=pub.isoformat(), source=src["name"], title=it["title"],
                            link=it["link"], level=level, relevance=cls["relevance"], lean=cls["lean"], cluster=c[2], pushed=int(push), price_at=price))
        if push:
            c[4] = level
            srcs = "、".join(sorted(c[3]))
            extra = f"（{n_src} 家来源报道）" if corroborated else ""
            body = (f"{cls['relevance']}相关 {cls['lean']} · 来源：{srcs} · 发布 {fmt(pub)}{extra}\n{it['title']}\n"
                    f"{it['summary'][:200]}\n{it['link']}\nBTC 当时 {price:,.0f}" if price else f"{it['title']}\n{it['link']}")
            self.n.send(level, f"[{src['name']}] {it['title'][:90]}", body, "新闻")


# ---------------- 盘面异常 ----------------

class Market:
    def __init__(self, notifier):
        self.n = notifier
        self.price = None
        self.oi_hist = []
        self.state = {}
        self.cool = {}
        self.pos_at = 0
        self.slope_state = None
        self.slope_bar = None
        self.minute_seen = {}

    def _cooldown(self, key, sec=1800):
        if time.time() - self.cool.get(key, 0) < sec:
            return False
        self.cool[key] = time.time()
        return True

    def poll(self):
        try:
            self.check_btc()
        except Exception as ex:
            log("BTC 行情获取失败:", ex)
        try:
            self.check_spec_anomaly()
        except Exception as ex:
            log("盘面异动获取失败:", ex)
        try:
            self.check_cross()
        except Exception as ex:
            log("跨市场行情获取失败:", ex)
        try:
            self.check_stocks()
        except Exception as ex:
            log("美股官方价获取失败:", ex)
        try:
            self.check_position()
        except Exception as ex:
            log("仓位结构获取失败:", ex)

    def check_btc(self):
        kl = MD.klines("1m", 1500)
        kl = kl[:-1]  # 去掉未走完的这一根
        hi = [float(k[2]) for k in kl]; lo = [float(k[3]) for k in kl]; cl = [float(k[4]) for k in kl]; vol = [float(k[5]) for k in kl]
        self.price = cl[-1]
        r5 = [(max(hi[i:i + 5]) - min(lo[i:i + 5])) / cl[i] * 100 for i in range(len(kl) - 1445, len(kl) - 5, 5)]
        base5 = statistics.median(r5)
        now5 = (max(hi[-5:]) - min(lo[-5:])) / cl[-6] * 100
        move15 = (cl[-1] / cl[-16] - 1) * 100
        vbase = statistics.median(vol[-1440:])
        prev24 = (max(hi[-1440:]) - min(lo[-1440:])) / cl[-1440] * 100
        self.state.update(price=self.price, range5=now5, base5=base5, move15=move15, prev24=prev24, vol_ratio=vol[-1] / vbase if vbase else 0)

        if now5 >= max(4 * base5, 0.5) and self._cooldown("btc_range", 900):
            lvl = "red" if now5 >= 1.2 else "yellow"
            d = "上涨" if cl[-1] > cl[-6] else "下跌"
            self.n.send(lvl, f"BTC 5 分钟急速{d}：振幅 {now5:.2f}%（平时约 {base5:.2f}%）",
                        f"现价 {self.price:,.0f}，15 分钟 {move15:+.2f}%。可能有突发消息，先看新闻时间线，别急着进场。", "盘面异常")
        oi = MD.open_interest()
        self.oi_hist.append((time.time(), oi, self.price))
        self.oi_hist = [x for x in self.oi_hist if time.time() - x[0] < 3600]
        old = [x for x in self.oi_hist if time.time() - x[0] >= 900]
        if old:
            t0, oi0, p0 = old[-1]
            doi, dp = (oi / oi0 - 1) * 100, (self.price / p0 - 1) * 100
            self.state.update(oi=oi, oi_chg15=doi)
        else:
            self.state.update(oi=oi)

        fr = MD.funding_pct()
        self.state.update(funding=fr)
        try:
            ls = MD.long_short(4)
            self.state.update(ls=ls[-1], ls_chg15=ls[-1] - ls[0])
        except Exception:
            pass
        path = os.path.join(OUT, "market_log.csv")
        new = not os.path.exists(path)
        with open(path, "a", encoding="utf-8") as f:
            if new:
                f.write("time,price,open_interest,funding_pct,long_short_ratio\n")
            f.write(f"{now_utc().isoformat()},{self.price},{oi},{fr},{self.state.get('ls', '')}\n")

    def _minute(self, spec, row, prior_rows, prev_row):
        bar = dict(t=int(row[0]), c=float(row[4]), v=float(row[5]))
        hit = SL.minute_spike([float(x[5]) for x in prior_rows], [float(prev_row[4])], bar, spec)
        if hit["vol"] and self.minute_seen.get("vol") != bar["t"]:
            self.minute_seen["vol"] = bar["t"]
            self.n.send("yellow", f"1 分钟成交量是前 2 小时中位数的 {hit['vol_ratio']:.1f} 倍",
                        f"这一分钟涨跌 {hit['move_pct']:+.2f}%。量达到 6 倍就报，不要求同时大涨大跌。只是提醒，不会下单。", "盘面异动")
        if hit["move"] and self.minute_seen.get("move") != bar["t"]:
            self.minute_seen["move"] = bar["t"]
            self.n.send("yellow", f"1 分钟涨跌 {hit['move_pct']:+.2f}%",
                        "单分钟涨跌到了 0.5%。只是提醒，不会下单。", "盘面异动")

    def check_spec_anomaly(self):
        """每分钟：1 分钟量或涨跌、5 分钟阶梯三连阳、5 分钟 MA7 斜率转折。"""
        spec = SL.load_spec()
        kl = MD.spot_klines("1m", spec["anomaly"]["vol_median_bars"] + 5)
        closed = kl[:-1]
        if len(closed) > spec["anomaly"]["vol_median_bars"] + 1:
            n = spec["anomaly"]["vol_median_bars"]
            self._minute(spec, kl[-1], closed[-n:], closed[-1])
            self._minute(spec, closed[-1], closed[-n - 1:-1], closed[-2])
        k5 = MD.spot_klines("5m", 40)
        bars = [dict(t=int(x[0]), o=float(x[1]), h=float(x[2]), l=float(x[3]), c=float(x[4]), v=float(x[5])) for x in k5]
        trio = bars[-3]["t"] if len(bars) >= 8 else None
        if trio is not None and SL._three_bull(bars, spec):
            if self.minute_seen.get("three") != trio:
                self.minute_seen["three"] = trio
                self.n.send("yellow", "5 分钟阶梯三连阳",
                            "连续 3 根阳线，实体都不小于全幅的 40%，收盘逐根抬高，回踩不深过前一根全幅的一半，合计涨幅不少于 0.30%。只是形态提醒，没有对应的三连阴。不会下单。",
                            "盘面异动")
        elif self.minute_seen.get("three") == trio:
            self.minute_seen["three"] = None
        if len(bars) >= 10:
            closed_t = bars[-2]["t"]
            if self.slope_bar != closed_t:
                cc = [b["c"] for b in bars[:-1]]
                ma_now, ma_prev = sum(cc[-7:]) / 7, sum(cc[-8:-1]) / 7
                slope = (ma_now / ma_prev - 1) * 100 if ma_prev else 0.0
                self.slope_state, events = SL.step_ma7(self.slope_state, slope, spec)
                self.slope_bar = closed_t
                self.state["ma7_slope"] = slope
                self.state["ma7_mode"] = self.slope_state["mode"]
                for ev in events:
                    title = "5 分钟 MA7 斜率放缓" if ev["kind"] == "slow" else "5 分钟 MA7 斜率接近水平"
                    self.n.send("yellow", title, ev["text"] + "不会下单。", "盘面异动")

    def _liq_1h(self):
        spec = SL.load_spec()
        found = []
        path = os.path.join(OUT, "liq.json")
        try:
            if os.path.exists(path) and time.time() - os.path.getmtime(path) < 1200:
                data = json.load(open(path, encoding="utf-8"))
                now, lo, sh = time.time() * 1000, 0.0, 0.0
                for row in data.get("bars") or []:
                    if now - row[0] <= 3600e3:
                        lo += row[1]
                        sh += row[2]
                if lo + sh > 0:
                    found.append(dict(long=lo, short=sh, source="全网（Coinalyze）"))
        except Exception as ex:
            log("读取全网爆仓失败:", ex)
        try:
            found.append(MD.okx_liq_1h())
        except Exception as ex:
            log("OKX 爆仓获取失败:", ex)
        self.state["liq_1h"] = found
        for src in found:
            hit = SL.liq_hit(src["long"], src["short"], spec)
            if hit:
                hit["source"] = src["source"]
                return hit
        return None

    def _taker_text(self):
        t = self.state.get("taker")
        if not t:
            return ""
        return f"近 1 小时主动买 {t['buy'] / 1e6:.1f} 百万美元，主动卖 {t['sell'] / 1e6:.1f} 百万美元。"

    def check_position(self):
        """每 15 分钟看一次仓位结构。每一类单独冷却 2 小时。"""
        spec = SL.load_spec()
        if time.time() - self.pos_at < spec["position_every_sec"]:
            return
        self.pos_at = time.time()
        p, cool = spec["position"], spec["position_cooldown_sec"]
        try:
            self.state["taker"] = MD.okx_taker_1h()
        except Exception as ex:
            log("主动买卖量获取失败:", ex)
        try:
            oi, px = MD.okx_coin_oi_change(), MD.spot_change_1h()
            self.state.update(oi_coin=oi["now"], oi_chg1h=oi["chg"], px_chg1h=px)
            if abs(oi["chg"]) >= p["oi_1h_pct"] and self._cooldown("oi_1h", cool):
                self.n.send("yellow", f"币本位持仓 1 小时 {oi['chg']:+.2f}%，价格 {px:+.2f}%",
                            SL.quadrant(px, oi["chg"]) + "。" + self._taker_text() + "只是提醒，不会下单。", "仓位异动")
        except Exception as ex:
            log("币本位持仓获取失败:", ex)
        try:
            fr = self.state.get("funding")
            if fr is None:
                fr = MD.funding_pct()
                self.state["funding"] = fr
            if abs(fr) >= p["funding_abs"] and self._cooldown("funding", cool):
                self.n.send("yellow", f"资金费率 {fr:+.4f}% / 8 小时",
                            "绝对值到了 0.03%/8 小时。只是提醒，不会下单。", "仓位异动")
        except Exception as ex:
            log("资金费率获取失败:", ex)
        try:
            ls = MD.okx_ls_1h()
            self.state.update(ls_1h=ls["now"], ls_chg1h=ls["chg"])
            if abs(ls["chg"]) >= p["ls_1h_abs"] and self._cooldown("ls_1h", cool):
                way = "上升" if ls["chg"] > 0 else "下降"
                self.n.send("yellow", f"多空比 1 小时{way} {ls['chg']:+.2f}（现 {ls['now']:.2f}）",
                            "变化达到 0.10。" + self._taker_text() + "只是提醒，不会下单。", "仓位异动")
        except Exception as ex:
            log("多空比获取失败:", ex)
        try:
            hit = self._liq_1h()
            if hit and self._cooldown("liq_1h", cool):
                self.n.send("yellow", f"近 1 小时爆仓 {hit['total'] / 1e6:.1f} 百万美元，{hit['side']}占 {hit['frac'] * 100:.0f}%",
                            f"来源：{hit['source']}。单边不少于 70%，合计不少于 1500 万美元。只是提醒，不会下单。", "仓位异动")
        except Exception as ex:
            log("爆仓统计失败:", ex)

    def check_cross(self):
        syms = ",".join(urllib.parse.quote(s) for s in S.CROSS_ASSETS)
        res = get_json(S.YAHOO_SPARK.format(syms=syms))["spark"]["result"]
        cross = {}
        for r in res:
            sym = r["symbol"]
            name, th = S.CROSS_ASSETS.get(sym, (sym, 1))
            q = r["response"][0]
            closes = [c for c in (q.get("indicators", {}).get("quote") or [{}])[0].get("close") or [] if c is not None]   # Yahoo 偶尔返回空数据
            ts = q.get("timestamp", [])
            if len(closes) < 4 or not ts or time.time() - ts[-1] > 900:
                cross[name] = None  # 休市或数据过旧
                continue
            ch5 = (closes[-1] / closes[-2] - 1) * 100
            ch15 = (closes[-1] / closes[-4] - 1) * 100
            cross[name] = dict(last=closes[-1], ch15=ch15)
            if (abs(ch5) >= th or abs(ch15) >= th * 1.5) and self._cooldown("x_" + sym, 1800):
                self.n.send("yellow", f"{name} 异动：5 分钟 {ch5:+.2f}%，15 分钟 {ch15:+.2f}%",
                            f"BTC 现价 {self.price:,.0f}。跨市场同时异动通常意味着宏观消息，先查新闻。" if self.price else "", "跨市场")
        self.state["cross"] = cross

    def check_stocks(self):
        """Yahoo 官方价：上一个常规收盘、盘前 / 盘后最后一笔、含夜盘的全时段最新价。"""
        out = {}
        for sym in S.US_STOCKS:
            try:
                r = get_json(S.YAHOO_CHART.format(sym=sym), timeout=10)["chart"]["result"][0]
            except Exception:
                continue
            m, ts = r["meta"], r.get("timestamp") or []
            closes = r["indicators"]["quote"][0].get("close") or []
            last = next(((t, c) for t, c in zip(reversed(ts), reversed(closes)) if c is not None), None)
            per = m.get("currentTradingPeriod", {})
            reg_end = per.get("regular", {}).get("end", 0)
            ext = None
            if last and last[0] >= reg_end:
                ext = dict(price=last[1], at_ms=last[0] * 1000, session="盘后")
            elif last and last[0] < per.get("regular", {}).get("start", 0):
                ext = dict(price=last[1], at_ms=last[0] * 1000, session="盘前")
            out[sym] = dict(regular=m.get("regularMarketPrice"), regular_at_ms=(m.get("regularMarketTime") or 0) * 1000,
                            prev_close=m.get("chartPreviousClose") or m.get("previousClose"), ext=ext,
                            fullday=m.get("fulldayPrice"))
        self.state["stocks"] = dict(updated_ms=int(time.time() * 1000), data=out)

    def ruler(self):
        n = now_utc().astimezone(NY)
        if n.weekday() >= 5:
            sess = "周末"
        elif n.hour == 9:
            sess = "美股开盘那1小时"
        elif 8 <= n.hour < 16:
            sess = "美股交易时段"
        else:
            sess = "工作日非美股时段"
        p24 = self.state.get("prev24")
        if p24 is None or not self.price:
            return None
        tier = 0 if p24 < 2.44 else 1 if p24 < 3.77 else 2
        m1, p4 = RULER[sess][tier]
        return dict(session=sess, activity=["平静", "一般", "活跃"][tier], h1_median_pct=m1, h4_p80_pct=p4,
                    h1_median_pts=round(self.price * m1 / 100), h4_p80_pts=round(self.price * p4 / 100))


# ---------------- 经济日历 ----------------

class Calendar:
    def __init__(self, store, notifier):
        self.store, self.n = store, notifier
        self.events = [dict(title=t, country="USD", impact="High", at=parse_date(at), forecast="", previous="") for t, at in S.SCHEDULED_EVENTS]
        self.next_fetch = 0

    def poll(self):
        if time.time() >= self.next_fetch:
            try:
                data = get_json(S.CALENDAR_URL)
                self.events = [dict(title=e["title"], country=e["country"], impact=e["impact"], at=parse_date(e["date"]),
                                    forecast=e.get("forecast", ""), previous=e.get("previous", ""))
                               for e in data if e.get("country") == "USD" and e.get("impact") == "High"]
                self.events += [dict(title=t, country="USD", impact="High", at=parse_date(at), forecast="", previous="")
                                for t, at in S.SCHEDULED_EVENTS
                                if not any(abs((parse_date(at) - e["at"]).total_seconds()) < 3600 for e in self.events if e["at"])]
                self.next_fetch = time.time() + S.CALENDAR_EVERY
            except Exception as ex:
                log("经济日历获取失败:", ex)
                self.next_fetch = time.time() + 900
        now = now_utc()
        for e in self.events + self.us_opens():
            if not e["at"]:
                continue
            mins = (e["at"] - now).total_seconds() / 60
            tag = f"{e['title']}@{e['at'].isoformat()}"
            if 55 <= mins <= 60 and self.store.flag("60_" + tag):
                self.n.send("yellow", f"1 小时后：{e['title']}（{fmt(e['at'])}）",
                            f"预期 {e.get('forecast') or '—'}，前值 {e.get('previous') or '—'}。发布前后振幅通常是平时的 2 倍左右，先别开新仓或放宽止损。", "事件日历")
            if 0 < mins <= 10 and self.store.flag("10_" + tag):
                self.n.send("red", f"10 分钟后：{e['title']}（{fmt(e['at'])}）", "不开新仓；已有仓位确认止损已挂。", "事件日历")

    def us_opens(self):
        """未来 3 天的美股开盘时间（跳过周末和 NYSE 休市日）。"""
        out = []
        n = now_utc().astimezone(NY)
        for d in range(0, 3):
            day = (n + timedelta(days=d)).date()
            if datetime(day.year, day.month, day.day).weekday() < 5 and day.isoformat() not in S.US_MARKET_HOLIDAYS:
                at = datetime(day.year, day.month, day.day, 9, 30, tzinfo=NY).astimezone(timezone.utc)
                out.append(dict(title="美股开盘", at=at, forecast="", previous=""))
        return out

    def upcoming(self, k=6):
        now = now_utc()
        ev = sorted([e for e in self.events + self.us_opens() if e["at"] and e["at"] > now - timedelta(minutes=30)], key=lambda e: e["at"])
        return [dict(title=e["title"], at=fmt(e["at"]), at_ms=int(e["at"].timestamp() * 1000), in_min=round((e["at"] - now).total_seconds() / 60))
                for e in ev[:k]]


# ---------------- 事件日志回填 ----------------

def backfill(store):
    rows = store.db.execute("SELECT key, first_seen, price_at, ret_1h, ret_4h FROM items WHERE level IN ('red','yellow','gray') "
                            "AND price_at IS NOT NULL AND (ret_1h IS NULL OR ret_4h IS NULL) LIMIT 50").fetchall()
    for key, fs, p0, r1, r4 in rows:
        t = datetime.fromisoformat(fs)
        for hours, col, cur in ((1, "ret_1h", r1), (4, "ret_4h", r4)):
            if cur is not None or now_utc() - t < timedelta(hours=hours):
                continue
            ms = int((t + timedelta(hours=hours)).timestamp() * 1000)
            try:
                k = MD.klines("1m", 1, start_ms=ms)
                if k:
                    store.db.execute(f"UPDATE items SET {col}=? WHERE key=?", ((float(k[0][1]) / p0 - 1) * 100, key))
            except Exception:
                pass
    store.db.commit()


# ---------------- 状态页 ----------------

def write_status(market, cal, notifier, store):
    st = dict(updated=fmt(now_utc()), tz=str(TZ), price=market.price, market=market.state, ruler=market.ruler(),
              upcoming=cal.upcoming(), alerts=notifier.recent[:30])
    latest = store.db.execute("SELECT first_seen, source, title, link, level, relevance, lean, title_zh, reason, lean_src FROM items "
                              "WHERE level IN ('red','yellow','gray') ORDER BY first_seen DESC LIMIT 40").fetchall()
    st["news"] = [dict(time=fmt(datetime.fromisoformat(r[0])), source=r[1], title=r[2], link=r[3], level=r[4], relevance=r[5], lean=r[6],
                       title_zh=r[7] or "", reason=r[8] or "", lean_src="AI" if r[9] == "AI" else "规则") for r in latest]
    with open(os.path.join(OUT, "status.json"), "w", encoding="utf-8") as f:
        json.dump(st, f, ensure_ascii=False, indent=1, default=str)

    e = html.escape
    m = market.state
    ru = st["ruler"] or {}
    cross = m.get("cross", {})
    color = {"red": "#d03b3b", "yellow": "#d99100", "gray": "#8a929c"}
    rows = "".join(f"<tr><td>{e(n['time'])}</td><td><span style='color:{color[n['level']]}'>●</span> {e(n['source'])}</td>"
                   f"<td><a href='{e(n['link'])}' target='_blank'>{e(n['title_zh'] or n['title'])}</a><div style='font-size:12px;color:#8a929c'>{e(n['reason'])}</div></td>"
                   f"<td style='white-space:nowrap'>{e(n['lean'])}<div style='font-size:11px;color:#8a929c'>{e(n['lean_src'])}判断 · {e(n['relevance'])}相关</div></td></tr>" for n in st["news"])
    al = "".join(f"<li><span style='color:{color[a['level']]}'>●</span> {e(a['time_local'])} [{e(a['kind'])}] {e(a['title'])}</li>" for a in st["alerts"][:15]) or "<li>暂无</li>"
    up = "".join(f"<li>{e(u['at'])} · {e(u['title'])} · {'进行中' if u['in_min'] <= 0 else str(u['in_min']) + ' 分钟后'}</li>" for u in st["upcoming"])
    cx = " · ".join(f"{k} {v['ch15']:+.2f}%" if v else f"{k} 休市" for k, v in cross.items())
    page = f"""<!doctype html><html lang="zh"><head><meta charset="utf-8"><meta http-equiv="refresh" content="30">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>BTC 实时监控</title>
<style>body{{font-family:-apple-system,"PingFang SC",sans-serif;margin:0;padding:16px;background:#f6f7f9;color:#15181c}}
.g{{display:grid;grid-template-columns:repeat(auto-fit,minmax(260px,1fr));gap:12px}}.c{{background:#fff;border:1px solid #e3e6ea;border-radius:6px;padding:12px 14px}}
h1{{font-size:20px;margin:0 0 12px}}h2{{font-size:14px;color:#68707a;margin:0 0 6px;font-weight:500}}b{{font-size:22px}}
table{{width:100%;border-collapse:collapse;font-size:13px}}td{{padding:6px 8px;border-top:1px solid #eef0f2;vertical-align:top}}a{{color:#1d5fb4;text-decoration:none}}
ul{{margin:0;padding-left:18px;font-size:13px}}.w{{overflow-x:auto}}@media(prefers-color-scheme:dark){{body{{background:#121416;color:#eceef0}}.c{{background:#1a1d20;border-color:#2c3136}}td{{border-color:#23272b}}a{{color:#6aa6f0}}}}</style></head><body>
<h1>BTC 实时监控 · 更新于 {e(st['updated'])}（{e(st['tz'])}）</h1>
<div class="g">
<div class="c"><h2>BTCUSDT 永续</h2><b>{(market.price or 0):,.0f}</b><div>15 分钟 {m.get('move15', 0):+.2f}% · 5 分钟振幅 {m.get('range5', 0):.2f}%（平时 {m.get('base5', 0):.2f}%）</div></div>
<div class="c"><h2>预期振幅（{e(ru.get('session',''))} · 过去 24h {e(ru.get('activity',''))}）</h2><b>1h ≈ {ru.get('h1_median_pts','—')} 点</b><div>4h 内 80% 情况下不超过 {ru.get('h4_p80_pts','—')} 点。止损小于 1h 中位数容易被扫。</div></div>
<div class="c"><h2>仓位</h2><div>{"币本位持仓 " + format(m.get("oi_coin") or 0, ",.0f") + " BTC（1 小时 " + format(m.get("oi_chg1h") or 0, "+.2f") + "%）" if m.get("oi_coin") else "持仓量 " + format(m.get("oi") or 0, ",.0f") + " BTC（15 分钟 " + format(m.get("oi_chg15") or 0, "+.2f") + "%）"}</div><div>资金费率 {m.get('funding', 0):+.4f}% / 8 小时 · 多空比 {m.get('ls_1h', m.get('ls', 0)):.2f}（1 小时 {m.get('ls_chg1h', 0):+.2f}）</div></div>
<div class="c"><h2>跨市场（15 分钟）</h2><div style="font-size:13px">{e(cx) or '—'}</div></div>
<div class="c"><h2>即将发生</h2><ul>{up}</ul></div>
<div class="c"><h2>最近警报</h2><ul>{al}</ul></div>
</div>
<div class="c w" style="margin-top:12px"><h2>新闻时间线（红 = 可能马上大波动，黄 = 今天内可能影响，灰 = 背景）</h2><table>{rows}</table></div>
<p style="font-size:12px;color:#8a929c">利多 / 利空由关键词规则判断，仅供参考；事件日志会记录实际涨跌用于检验。本页只提供信息，不构成投资建议。</p></body></html>"""
    with open(os.path.join(OUT, "status.html"), "w", encoding="utf-8") as f:
        f.write(page)


# ---------------- 主循环 ----------------

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true", help="跑一轮就退出")
    ap.add_argument("--minutes", type=float, default=0, help="运行多少分钟后退出（0 = 一直运行）")
    ap.add_argument("--no-push", action="store_true", help="不发邮件 / Telegram / Webhook，只写文件")
    args = ap.parse_args()
    os.makedirs(OUT, exist_ok=True)
    store = Store(os.path.join(OUT, "events.db"))
    notifier = Notifier(push=not args.no_push)
    market = Market(notifier)
    cal = Calendar(store, notifier)
    news = News(store, notifier, lambda: market.price)
    lab = LB.Labeler(store, OUT, log)
    from cloudstate import Outbox, StatePublisher, load_state, save_state
    outbox = Outbox(OUT, log)
    notifier.outbox = outbox
    load_state(OUT, news, market, notifier, log)

    def notify(title, body):
        if sys.platform == "darwin" and os.environ.get("MAC_NOTIFY") != "0":
            from paper import mac_notify
            mac_notify(title, body)
        outbox.add("red", "持仓", title, body)
    try:
        from paper import PaperTrader
        paper = PaperTrader(OUT, log, notify=notify)
        log("模拟盘已启动（规则 A / B，参数见 rules_spec.json）")
    except Exception as ex:
        paper = None
        log("模拟盘未启动（需要 pip install -r requirements.txt）:", ex)
    try:
        from zhang_alert import ZhangAlert
        zalert = ZhangAlert(OUT, notifier.send, log, calls=paper.st["model_calls"] if paper else None)
    except Exception as ex:
        zalert = None
        log("张主任规则提醒未启动:", ex)
    from publish import Publisher
    from liquidations import Liquidations
    pub = Publisher(OUT, log)
    spub = StatePublisher(OUT, log)
    log(f"提醒发件箱：{'私有仓库 ' + outbox.repo + ' 的 alerts.json（Muse 读取）' if outbox.enabled else '只记在本地 out/outbox.json'}")
    log(f"启动。输出目录 {OUT}，时区 {TZ}。首轮抓到的新闻只入库不推送。")
    liq = Liquidations(OUT, log)
    end = time.time() + args.minutes * 60 if args.minutes else None
    last_backfill = 0
    while True:
        t0 = time.time()
        market.poll()
        news.poll()
        lab.run()
        if paper:
            paper.step()
        if zalert:
            zalert.step()
        cal.poll()
        liq.step()
        if time.time() - last_backfill > 600:
            backfill(store)
            last_backfill = time.time()
        notifier.flush(force=args.once)
        try:
            write_status(market, cal, notifier, store)
        except Exception as ex:
            log("状态页写入失败:", ex)
        pub.step()
        try:
            save_state(OUT, news, market, notifier)
        except Exception as ex:
            log("状态保存失败:", ex)
        outbox.flush()
        spub.step()
        if args.once or (end and time.time() >= end):
            notifier.flush(force=True)
            outbox.flush()
            save_state(OUT, news, market, notifier)
            if spub.enabled:
                try:
                    spub.push(); log("已保存状态，下一段接着跑")
                except Exception as ex:
                    log("退出前保存状态失败:", ex)
            break
        time.sleep(max(5, 60 - (time.time() - t0)))


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(0)
