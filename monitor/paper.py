"""模拟盘：按 rules_spec.json 的统一规则，用实时 K 线逐根执行规则 A / B，记录每一笔（和 backtest/rules_backtest.py 同口径）。

由 monitor.py 每轮调用 PaperTrader.step()；也可单独运行：python3 paper.py --once
依赖：pandas、numpy（见 requirements.txt），以及 ../backtest 里的 zhang.py、direction_model.py 和模型参数。
输出：out/paper.json（面板读取）、out/paper_state.json（重启后续跑）、
      out/model_calls.json（面板「此刻结论」每次出现参考信号，就记一条「模型」喊单，面板的喊单记录自动核对）
"""
import base64
import json
import os
import subprocess
import sys
import time
import urllib.request
from datetime import datetime, timezone

import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "backtest"))
import zhang  # noqa: E402
from direction_model import features  # noqa: E402

SPEC = json.load(open(os.path.join(ROOT, "rules_spec.json"), encoding="utf-8"))
WEIGHTS = json.load(open(os.path.join(ROOT, "backtest", "results", "direction_weights_full.json"), encoding="utf-8"))
MAKER, TAKER = SPEC["fees"]["maker_pct"], SPEC["fees"]["taker_pct"]
MODEL_WHO = "模型·此刻结论"


def cloud_calls():
    """私有记录仓库里的喊单（面板「连接云端」后写入）。需要环境变量 RECORDS_REPO 和 GITHUB_TOKEN，没配置就返回空。"""
    repo = os.environ.get("RECORDS_REPO")
    token = os.environ.get("RECORDS_TOKEN") or ("" if os.environ.get("GITHUB_ACTIONS") == "true" else os.environ.get("GITHUB_TOKEN"))
    if not repo or not token:
        return []
    req = urllib.request.Request(f"https://api.github.com/repos/{repo}/contents/calls.json",
                                 headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json", "User-Agent": "btc-paper/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            doc = json.loads(base64.b64decode(json.load(r)["content"]).decode("utf-8"))
    except Exception:
        return []
    # 程序记的单自己管卖出；实盘里非 BTC 的单没有行情可盯（实盘 BTC 单照常提醒止损止盈）
    return [c for c in doc.get("calls", []) if c.get("who") not in (MODEL_WHO, "张主任多周期信号") and c.get("ref") and (c.get("sym") or "BTCUSDT") == "BTCUSDT"]


def mac_notify(title, body):
    """在 Mac 上弹系统通知（只在本机运行时；设 MAC_NOTIFY=0 关闭）。"""
    if sys.platform != "darwin" or os.environ.get("MAC_NOTIFY") == "0":
        return
    esc = lambda x: x.replace("\\", "\\\\").replace('"', '\\"')
    try:
        subprocess.run(["osascript", "-e", f'display notification "{esc(body)}" with title "{esc(title)}"'], timeout=10, check=False)
    except Exception:
        pass


def p80_4h():
    """规则 A 的保险止损 = 4 小时 P80 逆向（ATR 倍数），与面板一致，取自 dashboard/live/context.js。"""
    try:
        src = open(os.path.join(ROOT, "dashboard", "live", "context.js"), encoding="utf-8").read()
        line = next(x for x in src.splitlines() if x.startswith("var VOLSTAT="))
        return json.loads(line[len("var VOLSTAT="):].rstrip(";"))["mae_atr"]["4 小时"]["p80"]
    except Exception:
        return 1.6



def fetch_5m():
    import market_data as MD           # 本机用币安永续；云上（MARKET_SOURCE=cloud）用币安公开行情镜像的现货
    k = MD.klines("5m", 1500)
    df = pd.DataFrame([[x[0], x[1], x[2], x[3], x[4], x[5], x[9]] for x in k],
                      columns=["open_time", "open", "high", "low", "close", "volume", "taker_buy_volume"])
    df["time"] = pd.to_datetime(df.open_time, unit="ms", utc=True)
    df = df.set_index("time").drop(columns=["open_time"]).astype(float)
    return df.iloc[:-1]  # 去掉未收盘的一根


def prob(f, name):
    m = WEIGHTS[name]
    x = ((f[m["cols"]] - np.array(m["mu"])) / np.array(m["sd"])).clip(-5, 5).fillna(0).to_numpy()
    return 1 / (1 + np.exp(-(m["w"][0] + x @ np.array(m["w"][1:]))))


def annotate(df):
    d = zhang.signals(zhang.add_indicators(df.copy()))
    f = features(df, 288).replace([np.inf, -np.inf], np.nan)
    d["p15"], d["p1h"], d["p4h"] = prob(f, "15 分钟"), prob(f, "1 小时"), prob(f, "4 小时")
    h1 = d[["close"]].resample("1h").last().dropna()
    h1["ma25"] = h1.close.rolling(25).mean()
    tr = pd.DataFrame({"up1h": (h1.close > h1.ma25) & (h1.ma25 > h1.ma25.shift(6)),
                       "dn1h": (h1.close < h1.ma25) & (h1.ma25 < h1.ma25.shift(6))})
    tr.index = tr.index + pd.Timedelta("1h")
    d = d.join(tr.reindex(d.index, method="ffill").fillna(False))
    ny = d.index.tz_convert("America/New_York")
    mins = ny.hour * 60 + ny.minute - 570
    d["weekend"] = ny.weekday >= 5
    d["lowvol"] = d.volume < 0.5 * d.volume.rolling(288).median()
    d["pre_open"] = (ny.weekday < 5) & (mins >= -SPEC["event_block_min"]) & (mins < 0)
    d["hi12"], d["lo12"] = d.high.rolling(12).max(), d.low.rolling(12).min()
    return d


class PaperTrader:
    def __init__(self, out_dir, log=print, notify=None):
        self.out, self.log = out_dir, log
        self.notify = notify or mac_notify       # 本机：Mac 通知；云上：写进提醒发件箱给 Muse
        self.state_path = os.path.join(out_dir, "paper_state.json")
        self.st = json.load(open(self.state_path)) if os.path.exists(self.state_path) else {
            "last_bar": None, "A": {"mode": "flat", "trades": [], "missed": 0}, "B": {"mode": "flat", "trades": []}}
        self.st.setdefault("sig_prev", {"A": None, "B": None})
        self.st.setdefault("model_calls", [])

    # ---------- 逐根推进 ----------
    def step(self):
        try:
            d = annotate(fetch_5m())
        except Exception as ex:
            self.log("模拟盘取数失败:", ex); return
        last = self.st["last_bar"]
        new = d if last is None else d[d.index > pd.Timestamp(last)]
        if last is None:
            new = d.iloc[-1:]  # 第一次启动：只从最新一根开始，不回放历史
        h1 = d[["high", "low"]].resample("1h").agg({"high": "max", "low": "min"})
        a1 = (h1.high - h1.low).rolling(24).mean()
        a1.index = a1.index + pd.Timedelta("1h")                   # 只用已收盘的 1h K 线
        atr1h = a1.reindex(d.index + pd.Timedelta("5min"), method="ffill").to_numpy()
        pos = {ts: i for i, ts in enumerate(d.index)}
        n_before = len(self.st["model_calls"])
        for t, bar in new.iterrows():
            self.log_signals(t, bar, atr1h[pos[t]])
            self.bar_a(t, bar)
            self.bar_b(t, bar)
            self.st["last_bar"] = t.isoformat()
        now_ms = time.time() * 1000
        for c in self.st["model_calls"][n_before:]:
            if now_ms - c["t"] < 15 * 60e3:   # 只提醒刚发生的（重启回放历史时不提醒）
                self.notify("模型买入提示", f"{'做多' if c['side'] == 'long' else '做空'} 参考价 {c['ref']:,.0f}，止损 {c['stop']:,.0f}" + (f"，止盈 {c['target']:,.0f}" if c["target"] else "") + f"，最长 {c['hz'] // 60} 小时")
        self.update_exits(d)
        self.watch_positions(d, atr1h[-1], self.st["model_calls"])   # 模型单和张主任信号单持有期间都发盘面提醒
        manual = cloud_calls()                # 人工喊单：只提醒（该卖了 / 盘面变化），记录由面板写
        if manual:
            self.notify_manual_exits(d, manual)
            self.watch_positions(d, atr1h[-1], manual)
        self.save(d)

    @staticmethod
    def fill_time(d, c):
        """进场时间：现价进 = 喊单时间；到价再进 = 有效时间内第一次碰到进场价（5 分钟 K 线），没碰到返回 None。"""
        if c.get("entry") != "limit":
            return c["t"]
        if c.get("cancel"):
            return None
        until = c["t"] + (c.get("validMin") or 60) * 6e4
        w = d[(d.index >= pd.Timestamp(c["t"], unit="ms", tz="UTC").floor("5min")) & (d.index < pd.Timestamp(until, unit="ms", tz="UTC"))]
        hit = w[(w.low <= c["ref"]) & (w.high >= c["ref"])]
        return max(int(hit.index[0].timestamp() * 1000), c["t"]) if len(hit) else None

    def notify_manual_exits(self, d, calls):
        """人工喊单碰到止损 / 止盈或到最长持有时间时发 Mac 通知（和面板同口径）。"""
        now_ms, sent = time.time() * 1000, self.st.setdefault("watch_sent", [])
        for c in calls:
            if c.get("sim"):   # 模拟盘的单由模拟盘自己按止盈止损平仓，这里不再喊「该卖了」（和面板一致）
                continue
            filled = self.fill_time(d, c)
            if filled is None:
                continue
            sgn, exp = (1 if c["side"] == "long" else -1), filled + c["hz"] * 6e4
            if c.get("manualExit") or now_ms - exp > 15 * 60e3:
                continue
            bars = d[(d.index >= pd.Timestamp(filled, unit="ms", tz="UTC").floor("5min")) & (d.index < pd.Timestamp(exp, unit="ms", tz="UTC"))]
            why = None
            for _, b in bars.iterrows():
                if c.get("stop") and ((b.low <= c["stop"]) if sgn > 0 else (b.high >= c["stop"])):
                    why = "触及止损"; break
                if c.get("target") and ((b.high >= c["target"]) if sgn > 0 else (b.low <= c["target"])):
                    why = "触及止盈"; break
            if why is None and now_ms >= exp:
                why = "到了最长持有时间"
            key = f"{c['id']}:sell"
            if why and key not in sent:
                sent.append(key)
                body = f"{c.get('who', '')} 的{'多' if sgn > 0 else '空'}单（进场 {c['ref']:,.0f}）{why}，现价 {float(d.close.iloc[-1]):,.0f}"
                self.notify("该卖了", body)
                self.log("该卖了：" + body)

    def watch_positions(self, d, atr, calls):
        """持仓中的模型单：盘面急动、接近止损、15 分钟内进入美股开盘或数据公布时段时发 Mac 通知（只提醒，卖出仍按计划，和面板同口径）。"""
        if not np.isfinite(atr):
            return
        now_ms, last = time.time() * 1000, d.iloc[-1]
        sent = self.st.setdefault("watch_sent", [])
        try:
            upcoming = json.load(open(os.path.join(self.out, "status.json"), encoding="utf-8")).get("upcoming", [])
        except Exception:
            upcoming = []
        for c in calls:
            filled = self.fill_time(d, c) if c.get("who") != MODEL_WHO else c["t"]
            if filled is None or c.get("exit") or c.get("manualExit") or filled > now_ms or now_ms > filled + c["hz"] * 6e4:
                continue
            name = "模型" if c.get("who") == MODEL_WHO else c.get("who", "")
            sgn, who = (1 if c["side"] == "long" else -1), f"{name}{'多' if c['side'] == 'long' else '空'}单（进场 {c['ref']:,.0f}）"
            msgs = []
            mv = float(d.close.iloc[-1] - d.open.iloc[-3])       # 最近 15 分钟
            if abs(mv) >= atr and d.index[-3].timestamp() * 1000 >= filled:
                against = (mv > 0) != (sgn > 0)
                msgs.append((f"fm-{d.index[-1].isoformat()}", f"{who}：盘面急{'涨' if mv > 0 else '跌'} {abs(mv):,.0f} 美元，{'不利' if against else '有利'}。回测里这时马上卖并不更好，止损没到就按计划。"))
            if c.get("stop") and abs(float(last.close) - c["stop"]) <= 0.3 * atr:
                msgs.append((f"near-{int(now_ms // 9e5)}", f"{who}：离止损 {c['stop']:,.0f} 只剩 {abs(float(last.close) - c['stop']):,.0f} 美元。"))
            for u in upcoming:
                if 0 < u.get("at_ms", 0) - now_ms <= 15 * 60e3 and u["at_ms"] < filled + c["hz"] * 6e4:
                    msgs.append((f"win-{u['at_ms']}", f"{who}：{round((u['at_ms'] - now_ms) / 6e4)} 分钟后{u['title']}，确认止损挂在交易所。"))
            for key, body in msgs:
                k = f"{c['id']}:{key}"
                if k not in sent:
                    sent.append(k)
                    self.notify("盘面提醒", body)
                    self.log("盘面提醒：" + body)
        self.st["watch_sent"] = sent[-300:]

    def update_exits(self, d):
        """模型喊单的卖出：止损 / 止盈先碰到哪个就在哪个卖，都没碰到就到最长持有时间卖（和面板同口径，面板用 1 分钟 K 线会更精确）。"""
        now_ms = time.time() * 1000
        for c in self.st["model_calls"]:
            if c.get("exit"):
                continue
            sgn, exp = (1 if c["side"] == "long" else -1), c["t"] + c["hz"] * 6e4
            bars = d[(d.index >= pd.Timestamp(c["t"], unit="ms", tz="UTC")) & (d.index < pd.Timestamp(exp, unit="ms", tz="UTC"))]
            ex = None
            for t, b in bars.iterrows():
                if c["stop"] and ((b.low <= c["stop"]) if sgn > 0 else (b.high >= c["stop"])):
                    ex = dict(t=int(t.timestamp() * 1000), px=c["stop"], why="止损"); break
                if c["target"] and ((b.high >= c["target"]) if sgn > 0 else (b.low <= c["target"])):
                    ex = dict(t=int(t.timestamp() * 1000), px=c["target"], why="止盈"); break
            if ex is None and now_ms >= exp + 5 * 60e3 and len(bars):
                ex = dict(t=int(exp), px=float(bars.close.iloc[-1]), why="到时间")
            if ex:
                ex["pnl"] = round(sgn * (ex["px"] / c["ref"] - 1) * 100, 4)
                c["exit"] = ex
                name = "模型" if c.get("who") == MODEL_WHO else c.get("who", "")
                self.log(f"{name}喊单卖出：{ex['why']}，{ex['pnl']:+.2f}%")
                if now_ms - ex["t"] < 15 * 60e3:
                    self.notify("该卖了", f"{name}{'多' if sgn > 0 else '空'}单（进场 {c['ref']:,.0f}）{ex['why']}，卖出价 {ex['px']:,.0f}，{ex['pnl']:+.2f}%")

    def signals(self, b):
        """这根收盘时规则 A / B 各自是否给出信号（不管模拟盘有没有持仓），口径与面板 ruleSigs 一致。"""
        a = b_ = None
        if not b.pre_open and not np.isnan(b.p4h):
            a = "long" if b.p4h >= SPEC["rule_a"]["p_long"] else "short" if b.p4h <= SPEC["rule_a"]["p_short"] else None
        pm = (b.p15 + b.p1h) / 2
        if not (b.weekend or b.lowvol or b.pre_open):
            if b.long_B and not b.dn1h and pm >= 0.5:
                b_ = "long"
            elif b.short_B and not b.up1h and pm <= 0.5:
                b_ = "short"
        return {"A": a, "B": b_}

    def log_signals(self, t, b, atr1h):
        """信号从无到有（或换方向）时记一条模型喊单；连续几根都是同一个信号只记第一根，同方向 60 分钟内不重复。"""
        sig, prev = self.signals(b), self.st["sig_prev"]
        close_t = t + pd.Timedelta("5min")
        for rule, side in sig.items():
            # 同一规则同方向 60 分钟内只记一次，避免信号断一根又出现时重复记
            recent = any(c["id"].startswith(f"m-{rule}-{side}-") and close_t.timestamp() * 1000 - c["t"] < 3600e3 for c in self.st["model_calls"][-20:])
            if side and side != prev.get(rule) and not recent:
                ref, sgn = float(b.close), 1 if side == "long" else -1
                if rule == "A":
                    stop_d = p80_4h() * atr1h
                    call = dict(stop=round(ref - sgn * stop_d, 1), target=0, hz=SPEC["rule_a"]["hold_bars"] * 5,
                                text=f"方案 A′（规则 A + 保险止损，未验证的变体）：4 小时模型上涨输出值 {b.p4h * 100:.1f}%（达到实验阈值，非盈利概率），挂限价 {ref:,.0f}，{SPEC['rule_a']['hold_bars'] * 5 // 60} 小时后平仓，保险止损按 4 小时 P80 逆向")
                else:
                    s = SPEC["rule_b"]
                    risk = max((ref - b.lo12 + s["stop_atr"] * b.atr) if sgn > 0 else (b.hi12 + s["stop_atr"] * b.atr - ref), ref * 0.001)
                    call = dict(stop=round(ref - sgn * risk, 1), target=round(ref + sgn * s["tp_R"] * risk, 1), hz=s["max_hold_bars"] * 5,
                                text=f"规则 B：交易原则完整{'做多' if sgn > 0 else '做空'}信号 + 模型同向 + 不逆 1h 趋势，市价进，止盈 {s['tp_R']}R")
                ms = int(close_t.timestamp() * 1000)
                self.st["model_calls"].append(dict(id=f"m-{rule}-{side}-{ms}", t=ms, who=MODEL_WHO, side=side, ref=round(ref, 1), refGiven=False, **call))
                self.log(f"模型喊单：规则 {rule} {'做多' if sgn > 0 else '做空'}，参考价 {ref:,.1f}")
            prev[rule] = side

    def bar_a(self, t, b):
        s, A = SPEC["rule_a"], self.st["A"]
        if A["mode"] == "pending":
            A["waited"] += 1
            if (b.low < A["px"]) if A["side"] == "long" else (b.high > A["px"]):
                A.update(mode="open", entry=A["px"], entry_time=t.isoformat(), held=0, fee=MAKER)
            elif A["waited"] >= s["limit_valid_bars"]:
                A["missed"] += 1; A["mode"] = "flat"
            return
        if A["mode"] == "open":
            A["held"] += 1
            sgn = 1 if A["side"] == "long" else -1
            if s.get("use_insurance_stop"):
                stop = A["entry"] * (1 - sgn * s["insurance_stop_pct"] / 100)
                if (b.low <= stop) if sgn > 0 else (b.high >= stop):
                    return self.close("A", t, stop, A["fee"] + TAKER, "保险止损")
            if A["held"] >= s["hold_bars"]:
                A.update(mode="exiting", exit_px=float(b.close), waited=0)
            return
        if A["mode"] == "exiting":
            A["waited"] += 1
            if (b.high > A["exit_px"]) if A["side"] == "long" else (b.low < A["exit_px"]):
                return self.close("A", t, A["exit_px"], A["fee"] + MAKER, "到期挂单平仓")
            if A["waited"] >= s["limit_valid_bars"]:
                return self.close("A", t, float(b.close), A["fee"] + TAKER, "到期市价平仓")
            return
        if b.pre_open or np.isnan(b.p4h):
            return
        side = "long" if b.p4h >= s["p_long"] else "short" if b.p4h <= s["p_short"] else None
        if side:
            A.update(mode="pending", side=side, px=float(b.close), waited=0, signal_time=t.isoformat(), p=round(float(b.p4h), 4))

    def bar_b(self, t, b):
        s, B = SPEC["rule_b"], self.st["B"]
        if B["mode"] == "pending":       # 市价单：在信号后下一根开盘成交
            sgn = 1 if B["side"] == "long" else -1
            px = float(b.open)
            risk = max((px - B["lo12"] + s["stop_atr"] * B["atr"]) if sgn > 0 else (B["hi12"] + s["stop_atr"] * B["atr"] - px), px * 0.001)
            B.update(mode="open", entry=px, entry_time=t.isoformat(), held=0, fee=TAKER, stop=px - sgn * risk, tp=px + sgn * s["tp_R"] * risk)
        if B["mode"] == "open":
            B["held"] += 1
            long_ = B["side"] == "long"
            if (b.low <= B["stop"]) if long_ else (b.high >= B["stop"]):
                return self.close("B", t, B["stop"], B["fee"] + TAKER, "止损")
            if (b.high >= B["tp"]) if long_ else (b.low <= B["tp"]):
                return self.close("B", t, B["tp"], B["fee"] + MAKER, "止盈")
            if B["held"] >= s["max_hold_bars"]:
                return self.close("B", t, float(b.close), B["fee"] + TAKER, "到期平仓")
            return
        pm = (b.p15 + b.p1h) / 2
        ok = not (b.weekend or b.lowvol or b.pre_open)
        side = None
        if ok and b.long_B and not b.dn1h and pm >= 0.5:
            side = "long"
        elif ok and b.short_B and not b.up1h and pm <= 0.5:
            side = "short"
        if side:
            B.update(mode="pending", side=side, signal_time=t.isoformat(), hi12=float(b.hi12), lo12=float(b.lo12), atr=float(b.atr))

    def close(self, rule, t, px, fee, why):
        R = self.st[rule]
        sgn = 1 if R["side"] == "long" else -1
        ret = sgn * (px / R["entry"] - 1) * 100 - fee
        R["trades"].append(dict(side=R["side"], signal_time=R.get("signal_time"), entry_time=R["entry_time"], entry=round(R["entry"], 1),
                                exit_time=t.isoformat(), exit=round(px, 1), ret_pct=round(ret, 4), why=why))
        R["trades"] = R["trades"][-500:]
        for k in ("side", "px", "waited", "entry", "entry_time", "held", "fee", "exit_px", "stop", "tp", "signal_time", "p", "hi12", "lo12", "atr"):
            R.pop(k, None)
        R["mode"] = "flat"
        self.log(f"模拟盘 规则{rule} 平仓：{why}，{ret:+.3f}%")

    # ---------- 输出 ----------
    def save(self, d):
        del self.st["model_calls"][:-800]     # 原地裁剪：张主任信号也写进这个列表（zhang_alert.py 拿着同一个列表），不能换成新列表
        json.dump(self.st, open(self.state_path, "w"), ensure_ascii=False, default=str)
        out = {"updated": datetime.now(timezone.utc).isoformat(), "last_bar": self.st["last_bar"], "price": float(d.close.iloc[-1]), "rules": {}}
        for k in ("A", "B"):
            R = self.st[k]; tr = R["trades"]
            rets = [x["ret_pct"] for x in tr]
            open_pos = {x: R[x] for x in ("side", "entry", "entry_time", "px", "stop", "tp", "held", "waited", "signal_time") if x in R}
            out["rules"][k] = dict(name=SPEC[f"rule_{k.lower()}"]["name"], mode=R["mode"], position=open_pos, missed=R.get("missed", 0),
                                   n=len(rets), win=round(sum(r > 0 for r in rets) / len(rets) * 100, 1) if rets else None,
                                   avg=round(float(np.mean(rets)), 4) if rets else None, cum=round(float(np.sum(rets)), 3) if rets else 0,
                                   backtest=SPEC[f"rule_{k.lower()}"]["backtest"], recent=tr[-30:][::-1])
        json.dump(out, open(os.path.join(self.out, "paper.json"), "w"), ensure_ascii=False, indent=1, default=str)
        json.dump({"updated": out["updated"], "calls": self.st["model_calls"]}, open(os.path.join(self.out, "model_calls.json"), "w"), ensure_ascii=False, indent=1)


if __name__ == "__main__":
    os.makedirs(os.path.join(HERE, "out"), exist_ok=True)
    p = PaperTrader(os.path.join(HERE, "out"))
    p.step()
    print(json.dumps(json.load(open(os.path.join(HERE, "out", "paper.json")))["rules"]["A"], ensure_ascii=False)[:400])
