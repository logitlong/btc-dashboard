"""现行规格的 5 分钟信号（2026-11-03）。

每 3 分钟看一次币安公开现货 K 线（含未收盘的那根）。做空六维、做多六维、自定义 6 项，
哪一项从没有变成有就记一条背景，一直满足不重复，失效后再出现可以再报。
共振只在档位升高时发邮件；档位回落或失效只告知、不发邮件。
「提示做多 / 提示做空」只用于第 3 档，大仓位字样只用于极端档。单项永远只是背景。
模拟盘仍然会把第 3 档及以上记成一条拿满 4 小时的记录，那是记录，不会下单。
"""
import json
import os
import time

import market_data as MD
import signal_logic as SL

ZH_WHO = "张主任多周期信号"
HOLD_MIN = 240
BG_KEYS = [i for i, _ in SL.SIX_SHORT + SL.SIX_LONG + SL.CUSTOM]


def _bars(interval, limit):
    rows = MD.spot_klines(interval, limit)
    return [dict(t=int(x[0]), o=float(x[1]), h=float(x[2]), l=float(x[3]), c=float(x[4]), v=float(x[5])) for x in rows]


def _fetch():
    return {
        "5m": _bars("5m", 300), "15m": _bars("15m", 120), "30m": _bars("30m", 120),
        "1h": _bars("1h", 120), "4h": _bars("4h", 130), "1d": _bars("1d", 130),
    }


def _fmt_num(x):
    return "—" if x is None else f"{x:.1f}"


def _detail(z):
    o = z["osc"]
    parts = [f"{name} RSI {_fmt_num(o[k]['rsi'])} / J {_fmt_num(o[k]['j'])}"
             for k, name in (("5m", "5 分"), ("15m", "15 分"), ("30m", "30 分"), ("1h", "1 小时"), ("4h", "4 小时"))]
    s1 = z["stage1h"]["name"] if z["stage1h"] else "—"
    s30 = z["stage30"]["name"] if z["stage30"] else "—"
    return (f"现价 {z['close']:,.0f}。{SL.direction_phrase(z)}"
            f"1 小时{s1}，30 分{s30}（都看价格和 MA25）。" + "；".join(parts) + "。只是提醒，不会下单。")


def _names(items):
    got = [x["name"] for x in items if x["on"]]
    return "、".join(got) if got else "没有"


class ZhangAlert:
    def __init__(self, out_dir, send, log=print, calls=None):
        self.path = os.path.join(out_dir, "zhang_alert_state.json")
        self.send, self.log, self.calls = send, log, calls
        try:
            self.st = json.load(open(self.path, encoding="utf-8"))
        except Exception:
            self.st = {}

    def _save(self):
        tmp = self.path + ".tmp"
        json.dump(self.st, open(tmp, "w", encoding="utf-8"), ensure_ascii=False)
        os.replace(tmp, self.path)

    def _seed(self, z):
        self.st["flags"] = z["flags"]
        self.st["tier_short"] = z["tiers"]["short"]["tier"]
        self.st["tier_long"] = z["tiers"]["long"]["tier"]
        self.st["called_short"] = z["tiers"]["short"]["tier"] >= 3 and not z["anom_s"]
        self.st["called_long"] = z["tiers"]["long"]["tier"] >= 3 and not z["anom_l"]
        self.st["anom_s"] = z["anom_s"]
        self.st["anom_l"] = z["anom_l"]
        self._save()
        self.log("5 分钟信号已按现行规格建立基线，这一轮不补发")

    def _background(self, z):
        prev = self.st.get("flags") or {}
        groups = (("做空六维", z["six_s"], z["six_s_n"]), ("做多六维", z["six_l"], z["six_l_n"]), ("自定义", z["custom"], sum(x["on"] for x in z["custom"])))
        for key in BG_KEYS:
            if z["flags"].get(key) and not prev.get(key):
                group, items, n = next((g, items, n) for g, items, n in groups if any(x["id"] == key for x in items))
                name = next(x["name"] for x in items if x["id"] == key)
                total = 6
                self.send("gray", f"背景参考 · {group} {n}/{total}",
                          f"刚满足「{name}」。现在满足的是：{_names(items)}。这一项只记背景。", "背景")

    def _tiers(self, z, spec):
        for side, key in (("short", "tier_short"), ("long", "tier_long")):
            new, old = z["tiers"][side]["tier"], self.st.get(key, 0)
            if new > old:
                kind = "极端" if new >= 5 else "共振"
                aligned = (side == "long" and z["daily"]["dir"] > 0) or (side == "short" and z["daily"]["dir"] < 0)
                level = "red" if new >= 5 or (new >= 3 and aligned) else "yellow"
                title = f"{SL.tier_subject(side, new)} · {SL.direction_subject(z)}"
                self.send(level, title, SL.tier_label(side, new, z["tiers"][side]["via"]) + "。" + _detail(z), kind)
            elif new < old and old > 0:
                if new <= 0:
                    body = f"共振档位失效：{SL.tier_subject(side, old)} 已经不成立。之后重新满足会再报。"
                else:
                    body = f"共振档位回落：从「{SL.tier_subject(side, old)}」降到「{SL.tier_subject(side, new)}」。"
                self.send("gray", "共振档位回落" if new else "共振档位失效", body + _detail(z), "共振")
            self.st[key] = new
            if new < 3:
                self.st["called_" + side] = False

    def _call(self, z):
        if self.calls is None:
            return
        for side in ("short", "long"):
            tier = z["tiers"][side]["tier"]
            anom = z["anom_s"] if side == "short" else z["anom_l"]
            if tier >= 3 and not anom and not self.st.get("called_" + side):
                t = int(time.time() * 1000)
                self.calls.append(dict(
                    id=f"z-{side}-{t}", t=t, who=ZH_WHO, side=side, ref=round(z["close"], 1), refGiven=False,
                    stop=0, target=0, hz=HOLD_MIN,
                    text=f"{SL.tier_subject(side, tier)}：{SL.direction_subject(z)}。按「四小时理论」拿满 4 小时卖，不设止损止盈。只是模拟记录，不会下单"))
                self.st["called_" + side] = True
                self.log(f"共振第 {tier} 档记一笔模拟：{'做多' if side == 'long' else '做空'}，参考价 {z['close']:,.1f}")

    def _anomaly(self, z):
        for side, key, word in (("short", "anom_s", "超买还在放量冲高"), ("long", "anom_l", "超卖还在放量下杀")):
            on = z[key]
            if on and not self.st.get(key):
                self.send("yellow", f"反常异动：{word}",
                          "两个周期已经同向出了区间，但这根还在放量创新高或新低，可能有场外资金在推。先别顺着共振去做。" + _detail(z),
                          "反常异动")
            self.st[key] = on

    def step(self):
        spec = SL.load_spec()
        if time.time() - self.st.get("last_check", 0) < spec["five_min_every_sec"]:
            return
        try:
            z = SL.evaluate(_fetch(), spec, now_ms=time.time() * 1000)
        except Exception as ex:
            self.log("5 分钟信号取数失败:", ex)
            return
        self.st["last_check"] = time.time()
        if not z:
            self.log("5 分钟信号 K 线还不够")
            self._save()
            return
        if "flags" not in self.st:
            self._seed(z)
            return
        self._background(z)
        self._tiers(z, spec)
        self._anomaly(z)
        self._call(z)
        self.st["flags"] = z["flags"]
        self._save()
