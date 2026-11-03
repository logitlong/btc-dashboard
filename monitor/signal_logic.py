"""2026-11-03 现行监控规格的计算（只提醒，不下单）。

面板 dashboard/live/model.js 的 evaluateSignals / stepMa7 与这里逐项对应，阈值只读 rules_spec.json 的 signals。
K 线最后一根可以是未收盘的。指标用和面板一样的递推（RSI6、KDJ 的 J、MACD 柱），不另收紧任何阈值。
"""
import json
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

SIX_SHORT = (
    ("s1", "量能顶背离"),
    ("s2", "J>100后拐头向下"),
    ("s3", "RSI6触及70"),
    ("s4", "MACD正柱见顶衰减"),
    ("s5", "MA7上穿后拐头准备下穿"),
    ("s6", "顶部长上影+十字星"),
)
SIX_LONG = (
    ("l1", "量能底背离"),
    ("l2", "J<0后拐头向上"),
    ("l3", "RSI6触及30"),
    ("l4", "MACD负柱向零收敛"),
    ("l5", "MA7下穿后拐头准备上穿"),
    ("l6", "底部长下影+十字星"),
)
CUSTOM = (
    ("c1", "近6根收盘都在MA7上"),
    ("c2", "MA7准备上穿或10根内刚上穿MA25"),
    ("c3", "J>100"),
    ("c4", "RSI6>70"),
    ("c5", "近3根量连续递减"),
    ("c6", "MACD正柱比上一根短"),
)


def load_spec(path=None):
    p = path or os.path.join(ROOT, "rules_spec.json")
    return json.load(open(p, encoding="utf-8"))["signals"]


def is_signal_email(kind, level, spec=None):
    """邮件只发规格里的三类，而且只发红/黄。档位回落和六维背景不是邮件。"""
    kinds = (spec or load_spec())["email_kinds"]
    return kind in kinds and level in ("red", "yellow")


def _ewm(arr, alpha):
    out, prev = [], None
    for x in arr:
        if x is None:
            out.append(prev)
            continue
        prev = x if prev is None else alpha * x + (1 - alpha) * prev
        out.append(prev)
    return out


def _rsi6(closes):
    up, dn = [None], [None]
    for i in range(1, len(closes)):
        d = closes[i] - closes[i - 1]
        up.append(max(d, 0.0))
        dn.append(max(-d, 0.0))
    u, d = _ewm(up, 1 / 6), _ewm(dn, 1 / 6)
    out = []
    for i in range(len(closes)):
        if not d[i]:
            out.append(None)
        else:
            out.append(100 - 100 / (1 + u[i] / d[i]))
    return out


def _kdj_j(bars):
    rsv = []
    for i, x in enumerate(bars):
        if i < 8:
            rsv.append(50.0)
            continue
        lo = min(bars[k]["l"] for k in range(i - 8, i + 1))
        hi = max(bars[k]["h"] for k in range(i - 8, i + 1))
        rsv.append(50.0 if hi == lo else (x["c"] - lo) / (hi - lo) * 100)
    k = _ewm(rsv, 1 / 3)
    d = _ewm(k, 1 / 3)
    return [3 * k[i] - 2 * d[i] for i in range(len(bars))]


def _macd_hist(closes):
    f, s = _ewm(closes, 2 / 13), _ewm(closes, 2 / 27)
    dif = [f[i] - s[i] for i in range(len(closes))]
    dea = _ewm(dif, 2 / 10)
    return [dif[i] - dea[i] for i in range(len(closes))]


def _sma(closes, n):
    out = [None] * len(closes)
    acc = 0.0
    for i, x in enumerate(closes):
        acc += x
        if i >= n:
            acc -= closes[i - n]
        if i >= n - 1:
            out[i] = acc / n
    return out


def _median(xs):
    s = sorted(xs)
    m = len(s) // 2
    if not s:
        return None
    return s[m] if len(s) % 2 else (s[m - 1] + s[m]) / 2


def _bias(closes, n, look):
    """均线现值相对 look 根之前。斜率为 0 视为走平，改看价格在均线上方还是下方。"""
    i = len(closes) - 1
    if i < n - 1 + look:
        return None

    def at(end):
        return sum(closes[end - n + 1:end + 1]) / n

    now, old = at(i), at(i - look)
    slope = now - old
    pct = (now / old - 1) * 100 if old else 0.0
    flat = slope == 0
    if slope > 0:
        direction = 1
    elif slope < 0:
        direction = -1
    else:
        direction = 1 if closes[i] > now else -1 if closes[i] < now else 0
    bias = "偏多" if direction > 0 else "偏空" if direction < 0 else "走平"
    return dict(ma=now, old=old, slope=slope, pct=pct, dir=direction, flat=flat, bias=bias, price=closes[i])


def _stage(bars, n):
    """价格在 MA 的哪一侧，加上这条均线的斜率：同向才是上涨/下跌，否则过渡震荡。"""
    closes = [b["c"] for b in bars]
    if len(closes) < n + 1:
        return None
    ma = sum(closes[-n:]) / n
    prev = sum(closes[-n - 1:-1]) / n
    slope = ma - prev
    px = closes[-1]
    if px > ma and slope > 0:
        name = "上涨"
    elif px < ma and slope < 0:
        name = "下跌"
    else:
        name = "过渡震荡"
    return dict(name=name, ma=ma, slope=slope, price=px)


def _div_side(bars, side, window, frac):
    """48 根窗口里的真新高/真新低，且这根的量低于近期高点（或低点）量的中位数的 frac。"""
    if len(bars) < window:
        return False
    w = bars[-window:]
    prev, cur = w[:-1], w[-1]
    vols = []
    if side == "high":
        if not (cur["h"] > max(b["h"] for b in prev)):
            return False
        for i in range(1, len(prev) - 1):
            if prev[i]["h"] >= prev[i - 1]["h"] and prev[i]["h"] >= prev[i + 1]["h"]:
                vols.append(prev[i]["v"])
        peak = max(range(len(prev)), key=lambda i: prev[i]["h"])
    else:
        if not (cur["l"] < min(b["l"] for b in prev)):
            return False
        for i in range(1, len(prev) - 1):
            if prev[i]["l"] <= prev[i - 1]["l"] and prev[i]["l"] <= prev[i + 1]["l"]:
                vols.append(prev[i]["v"])
        peak = min(range(len(prev)), key=lambda i: prev[i]["l"])
    vols.append(prev[peak]["v"])
    med = _median(vols)
    return bool(med) and cur["v"] < frac * med


def _j_turn(j, hot, cold, look, up):
    w = j[-look:]
    if len(w) < look or any(x is None for x in w):
        return False
    if up:
        return min(w) < cold and j[-1] > j[-2] and j[-1] > min(w)
    return max(w) > hot and j[-1] < j[-2] and j[-1] < max(w)


def _hist_decay(h, look, short):
    seg = h[-look:]
    if len(seg) < look or any(x is None for x in seg):
        return False
    if short:
        if seg[-1] <= 0:
            return False
        p = max(range(look - 1), key=lambda i: seg[i])
        if seg[p] <= 0 or seg[-1] >= seg[p] or seg[-1] >= seg[-2]:
            return False
        return all(seg[i] <= seg[i - 1] for i in range(p + 1, look))
    if seg[-1] >= 0:
        return False
    p = min(range(look - 1), key=lambda i: seg[i])
    if seg[p] >= 0 or seg[-1] <= seg[p] or seg[-1] <= seg[-2]:
        return False
    return all(seg[i] >= seg[i - 1] for i in range(p + 1, look))


def _crossed(fast, slow, i, up):
    if None in (fast[i], slow[i], fast[i - 1], slow[i - 1]):
        return False
    if up:
        return fast[i] > slow[i] and fast[i - 1] <= slow[i - 1]
    return fast[i] < slow[i] and fast[i - 1] >= slow[i - 1]


def _ma_after_cross(ma7, ma25, look, short):
    """上穿之后拐头准备下穿，含已经死叉。做多镜像：下穿之后准备上穿，含已经金叉。"""
    n = len(ma7)
    start = max(1, n - look)
    if short:
        found = any(_crossed(ma7, ma25, i, True) for i in range(start, n))
        if not found or ma7[-1] is None or ma25[-1] is None or ma7[-2] is None:
            return False
        if ma7[-1] < ma25[-1]:
            return True
        return (ma7[-1] - ma7[-2]) < 0
    found = any(_crossed(ma7, ma25, i, False) for i in range(start, n))
    if not found or ma7[-1] is None or ma25[-1] is None or ma7[-2] is None:
        return False
    if ma7[-1] > ma25[-1]:
        return True
    return (ma7[-1] - ma7[-2]) > 0


def _candle(bars, spec, top):
    cdl = spec["candle"]
    if len(bars) < cdl["top_bars"]:
        return False
    b = bars[-1]
    rng = b["h"] - b["l"]
    if rng <= 0:
        return False
    body = abs(b["c"] - b["o"])
    upper = b["h"] - max(b["o"], b["c"])
    lower = min(b["o"], b["c"]) - b["l"]
    if body > cdl["doji_body_frac"] * rng:
        return False
    window = bars[-cdl["top_bars"]:]
    if top:
        long_shadow = upper >= cdl["long_shadow_frac"] * rng and upper >= lower
        at_ext = b["h"] >= max(x["h"] for x in window)
        return long_shadow and at_ext
    long_shadow = lower >= cdl["long_shadow_frac"] * rng and lower >= upper
    at_ext = b["l"] <= min(x["l"] for x in window)
    return long_shadow and at_ext


def _out(rsi, j, side, bound):
    if rsi is None or j is None:
        return False, False
    if side == "short":
        return rsi > bound["rsi_ob"], j > bound["j_ob"]
    return rsi < bound["rsi_os"], j < bound["j_os"]


def _strict(rsi, j, side, bound):
    return _out(rsi, j, side, bound)


def resonance(oscs, div_s, div_l, spec):
    """共振漏斗。oscs 的键是 5m/15m/30m/1h/4h，值是 {rsi, j}。返回两边的档位。

    1 观察（5 分钟严格）  2 同指标配对（15 分钟用自己的边界，跨指标不算）
    3 提示（两边都到 5 分钟严格档）  4 强烈提示（3 再加上严格量价背离）
    5 极端：同一指标 5m/15m/30m/1h 都出各自边界  6 4 小时也出，再升一级
    """
    b = spec["bounds"]
    strict = b["5m"]

    def side_tier(side, div):
        r5, j5 = _out(oscs["5m"]["rsi"], oscs["5m"]["j"], side, b["5m"])
        r15, j15 = _out(oscs["15m"]["rsi"], oscs["15m"]["j"], side, b["15m"])
        r30, j30 = _out(oscs["30m"]["rsi"], oscs["30m"]["j"], side, b["30m"])
        r1, j1 = _out(oscs["1h"]["rsi"], oscs["1h"]["j"], side, b["1h"])
        r4, j4 = _out(oscs["4h"]["rsi"], oscs["4h"]["j"], side, b["4h"])
        rs5, js5 = _strict(oscs["5m"]["rsi"], oscs["5m"]["j"], side, strict)
        rs15, js15 = _strict(oscs["15m"]["rsi"], oscs["15m"]["j"], side, strict)
        via3 = []
        if rs5 and rs15:
            via3.append("RSI")
        if js5 and js15:
            via3.append("KDJ")
        t3 = bool(via3)
        via2 = []
        if r5 and r15:
            via2.append("RSI")
        if j5 and j15:
            via2.append("KDJ")
        t2 = bool(via2)
        via1 = []
        if r5:
            via1.append("RSI")
        if j5:
            via1.append("KDJ")
        t1 = bool(via1)
        funnel = 4 if t3 and div else 3 if t3 else 2 if t2 else 1 if t1 else 0
        via = via3 if t3 else via2 if t2 else via1
        ext_rsi = r5 and r15 and r30 and r1
        ext_j = j5 and j15 and j30 and j1
        ext_via = []
        if ext_rsi:
            ext_via.append("RSI")
        if ext_j:
            ext_via.append("KDJ")
        ext4 = (ext_rsi and r4) or (ext_j and j4)
        ext_rank = 6 if ext4 else 5 if ext_via else 0
        rank = max(funnel, ext_rank)
        if ext_rank >= funnel and ext_rank:
            via = ext_via
        if "RSI" in via:
            via = ["RSI"] + [x for x in via if x != "RSI"]
        return dict(tier=rank, via=via, funnel=funnel, extreme=ext_rank)

    return {"short": side_tier("short", div_s), "long": side_tier("long", div_l)}


def tier_label(side, tier, via):
    """给用户看的句子。只有第 3 档及以上才出现「提示做多/做空」或大仓位字样。"""
    word = "做多" if side == "long" else "做空"
    ob = "超卖" if side == "long" else "超买"
    via_txt = "、".join(via) if via else "指标"
    if tier <= 0:
        return "没有共振"
    if tier == 1:
        return f"观察：5 分钟严格{ob}（{via_txt}）。只是观察，不是提示{word}"
    if tier == 2:
        return f"同指标配对 · 观察：5 分钟和 15 分钟 {via_txt} 同时{ob}。只是配对，不是提示{word}"
    if tier == 3:
        return f"提示{word}：5 分钟和 15 分钟 {via_txt} 都到严格档"
    if tier == 4:
        return f"强烈提示{word}：共振，并且有严格量价{'底' if side == 'long' else '顶'}背离"
    base = "大仓位买入信号" if side == "long" else "大仓位做空信号"
    if tier == 5:
        return f"{base}：同一指标在 5 分、15 分、30 分、1 小时都出区间（{via_txt}）"
    return f"{base} · 再升一级：4 小时也出区间（{via_txt}）"


def tier_subject(side, tier):
    long = side == "long"
    if tier == 1:
        return "共振观察"
    if tier == 2:
        return "共振配对观察"
    if tier == 3:
        return "提示做多" if long else "提示做空"
    if tier == 4:
        return "强烈提示做多" if long else "强烈提示做空"
    if tier == 5:
        return "大仓位买入信号" if long else "大仓位做空信号"
    if tier >= 6:
        return "大仓位买入信号再升一级" if long else "大仓位做空信号再升一级"
    return "共振"


def direction_phrase(z):
    d, h = z["daily"], z["h4"]

    def one(name, x):
        extra = ""
        if x["flat"]:
            extra = "，走平，价格在均线" + ("上" if x["price"] > x["ma"] else "下")
        return f"{name}MA99{x['bias']}（斜率 {x['slope']:+.1f}，{x['pct']:+.2f}%{extra}）"

    rel = ""
    if d["dir"] and h["dir"] and d["dir"] * h["dir"] < 0:
        rel = " 冲突，以日线为准。"
    elif d["dir"] and d["dir"] == h["dir"]:
        rel = " 两层同向。"
    return one("日线", d) + "；" + one("4小时", h) + "。" + rel


def direction_subject(z):
    return f"日线MA99{z['daily']['bias']} · 4小时MA99{z['h4']['bias']}"


def _three_bull(bars, spec):
    p = spec["anomaly"]["three"]
    if len(bars) < 3:
        return False
    a, b, c = bars[-3], bars[-2], bars[-1]

    def bull(x):
        rng = x["h"] - x["l"]
        return x["c"] > x["o"] and rng > 0 and (x["c"] - x["o"]) >= p["body_frac"] * rng

    if not (bull(a) and bull(b) and bull(c)):
        return False
    if not (a["c"] < b["c"] < c["c"]):
        return False

    def overlap_ok(prev, nxt):
        rng = prev["h"] - prev["l"]
        return nxt["l"] >= prev["h"] - p["overlap_frac"] * rng

    if not (overlap_ok(a, b) and overlap_ok(b, c)):
        return False
    return a["o"] > 0 and (c["c"] / a["o"] - 1) * 100 >= p["total_pct"]


def _items(pairs, vals):
    return [dict(id=i, name=n, on=bool(vals[k])) for k, (i, n) in enumerate(pairs)]


def _osc_of(bars):
    closes = [b["c"] for b in bars]
    r, j, h = _rsi6(closes), _kdj_j(bars), _macd_hist(closes)
    return dict(rsi=r[-1], j=j[-1], hist=h, rsi_s=r, j_s=j, closes=closes)


def evaluate(bundle, spec, now_ms=None):
    """bundle: 5m/15m/30m/1h/4h/1d，每项是 [{t,o,h,l,c,v}]，最后一根可以未收盘。数据不够返回 None。"""
    need = {"5m": 80, "15m": 40, "30m": 40, "1h": 40, "4h": spec["h4"]["ma"] + spec["h4"]["lookback"] + 1,
            "1d": spec["daily"]["ma"] + spec["daily"]["lookback"] + 1}
    for k, n in need.items():
        if len(bundle.get(k) or []) < n:
            return None
    m5 = bundle["5m"]
    osc = {tf: _osc_of(bundle[k]) for tf, k in (("5m", "5m"), ("15m", "15m"), ("30m", "30m"), ("1h", "1h"), ("4h", "4h"))}
    six = spec["six"]
    closes = osc["5m"]["closes"]
    ma7, ma25 = _sma(closes, 7), _sma(closes, 25)
    div_s = _div_side(m5, "high", six["div_window"], six["div_vol_frac"])
    div_l = _div_side(m5, "low", six["div_window"], six["div_vol_frac"])
    j, h = osc["5m"]["j_s"], osc["5m"]["hist"]
    r = osc["5m"]["rsi"]
    s_on = [
        div_s,
        _j_turn(j, six["j_hot"], six["j_cold"], six["j_look"], False),
        r is not None and r >= six["rsi_touch_ob"],
        _hist_decay(h, six["hist_look"], True),
        _ma_after_cross(ma7, ma25, six["ma_cross_look"], True),
        _candle(m5, spec, True),
    ]
    l_on = [
        div_l,
        _j_turn(j, six["j_hot"], six["j_cold"], six["j_look"], True),
        r is not None and r <= six["rsi_touch_os"],
        _hist_decay(h, six["hist_look"], False),
        _ma_after_cross(ma7, ma25, six["ma_cross_look"], False),
        _candle(m5, spec, False),
    ]
    cu = spec["custom"]
    above = False
    if ma7[-1] is not None:
        above = all(ma7[i] is not None and closes[i] > ma7[i] for i in range(len(closes) - cu["closes_above"], len(closes)))
    gap = None
    prep = crossed = False
    if ma7[-1] is not None and ma25[-1] and ma7[-2] is not None:
        gap = (ma25[-1] - ma7[-1]) / ma25[-1]
        prep = ma7[-1] < ma25[-1] and 0 < gap < cu["ma_gap"] and (ma7[-1] - ma7[-2]) > 0
        look_from = max(0, len(closes) - 1 - cu["cross_look"])
        crossed = ma7[-1] > ma25[-1] and any(
            ma7[i] is not None and ma25[i] is not None and ma7[i] <= ma25[i] for i in range(look_from, len(closes) - 1))
    vols = [b["v"] for b in m5]
    c_on = [
        above,
        prep or crossed,
        osc["5m"]["j"] is not None and osc["5m"]["j"] > six["j_hot"],
        r is not None and r > six["rsi_touch_ob"],
        len(vols) >= 3 and vols[-1] < vols[-2] < vols[-3],
        h[-1] is not None and h[-2] is not None and h[-1] > 0 and h[-1] < h[-2],
    ]
    osc_now = {k: dict(rsi=v["rsi"], j=v["j"]) for k, v in osc.items()}
    tiers = resonance(osc_now, div_s, div_l, spec)
    rank_s, rank_l = tiers["short"]["tier"], tiers["long"]["tier"]
    if rank_s > rank_l:
        side = "short"
    elif rank_l > rank_s:
        side = "long"
    elif rank_s:
        side = "short" if (osc["5m"]["rsi"] or 50) >= 50 else "long"
    else:
        side = None
    tier = rank_s if side == "short" else rank_l if side == "long" else 0
    via = tiers[side]["via"] if side else []
    daily = _bias([b["c"] for b in bundle["1d"]], spec["daily"]["ma"], spec["daily"]["lookback"])
    h4 = _bias([b["c"] for b in bundle["4h"]], spec["h4"]["ma"], spec["h4"]["lookback"])
    if not daily or not h4:
        return None
    mid_n = spec["mid_ma"]
    stage1 = _stage(bundle["1h"], mid_n)
    stage30 = _stage(bundle["30m"], mid_n)
    closed = m5[:-1]
    closed_c = [b["c"] for b in closed]
    ma_now = sum(closed_c[-7:]) / 7
    ma_prev = sum(closed_c[-8:-1]) / 7
    slope = (ma_now / ma_prev - 1) * 100 if ma_prev else 0.0
    cur = m5[-1]
    el = 1.0
    if now_ms is not None and cur.get("t"):
        el = (now_ms - cur["t"]) / 300000
    pace = max(0.2, min(1.0, el if el > 0 else 1.0))
    v_now = cur["v"] / pace
    avg = sum(b["v"] for b in closed[-24:]) / min(24, len(closed[-24:]))
    hot = bool(avg) and v_now > 2 * avg and v_now > closed[-1]["v"]
    hi12 = max(b["h"] for b in closed[-12:])
    lo12 = min(b["l"] for b in closed[-12:])
    anom_s = rank_s >= 2 and hot and cur["h"] >= hi12
    anom_l = rank_l >= 2 and hot and cur["l"] <= lo12
    with_daily = bool(side) and tier >= 3 and ((side == "long" and daily["dir"] > 0) or (side == "short" and daily["dir"] < 0))
    six_s, six_l = _items(SIX_SHORT, s_on), _items(SIX_LONG, l_on)
    custom = _items(CUSTOM, c_on)
    flags = {x["id"]: x["on"] for x in six_s + six_l + custom}
    flags["three"] = _three_bull(m5, spec)
    return dict(
        close=cur["c"], bar=cur.get("t"), closed_t=closed[-1].get("t"),
        daily=daily, h4=h4, conflict=bool(daily["dir"] and h4["dir"] and daily["dir"] * h4["dir"] < 0),
        stage1h=stage1, stage30=stage30,
        osc=osc_now, div_s=div_s, div_l=div_l,
        six_s=six_s, six_l=six_l, custom=custom,
        six_s_n=sum(x["on"] for x in six_s), six_l_n=sum(x["on"] for x in six_l),
        tiers=tiers, tier=tier, tier_side=side, via=via,
        label=tier_label(side, tier, via) if side else "没有共振",
        extreme=side if tier >= 5 else None, extreme_plus=bool(side and tier >= 6),
        prompt=bool(side and tier >= 3), with_daily=with_daily,
        anom_s=bool(anom_s), anom_l=bool(anom_l),
        three_bull=flags["three"], ma7_slope=slope,
        ma7=ma7[-1], ma25=ma25[-1], flags=flags,
    )


def minute_spike(closed_vols, closed_prev_closes, bar, spec):
    """单分钟：量 ≥ 此前约 2 小时 1 分钟量中位数的倍数，或这一分钟涨跌达到阈值。两个条件彼此独立。"""
    p = spec["anomaly"]
    n = p["vol_median_bars"]
    prev = closed_vols[-n:]
    if len(prev) < n:
        return dict(vol=False, move=False, vol_ratio=None, move_pct=0.0)
    med = _median(prev)
    ratio = (bar["v"] / med) if med else None
    move = 0.0
    if closed_prev_closes:
        base = closed_prev_closes[-1]
        if base:
            move = (bar["c"] / base - 1) * 100
    return dict(vol=bool(ratio is not None and ratio >= p["vol_mult"]),
                move=abs(move) >= p["move_pct"], vol_ratio=ratio, move_pct=move)


def step_ma7(state, slope, spec):
    """5 分钟 MA7 斜率（%/根）。冲过阈值进入暴涨段并记住峰值；跌破峰值的 60% 且逐根走低报放缓；
    落到平坦阈值以内报接近水平，转负注明拐头。放缓或走平之后再冲过阈值，算新的一波。放缓≠反转。"""
    p = spec["anomaly"]["ma7"]
    st = dict(state or {})
    mode = st.get("mode") or "idle"
    peak = st.get("peak") or 0.0
    prev = st.get("prev")
    reported_slow = bool(st.get("reported_slow"))
    reported_flat = bool(st.get("reported_flat"))
    events = []
    declining = prev is not None and slope < prev
    if mode in ("idle", "slowing", "flat") and slope > p["surge_pct"]:
        mode, peak = "surge", slope
        reported_slow = reported_flat = False
    elif mode == "surge":
        peak = max(peak, slope)
        if slope <= p["flat_pct"] and not reported_flat:
            turn = "已转负，注明拐头。" if slope < 0 else ""
            events.append(dict(kind="flat", text=(
                f"5 分钟 MA7 斜率接近水平：{slope:.3f}%/根，已落到 +{p['flat_pct']:.2f}%/根以内。{turn}放缓≠反转。")))
            mode, reported_flat = "flat", True
        elif slope < p["peak_frac"] * peak and declining and not reported_slow:
            events.append(dict(kind="slow", text=(
                f"5 分钟 MA7 斜率放缓：峰值 {peak:.3f}%/根，现在 {slope:.3f}%/根，"
                f"低于峰值的 {p['peak_frac'] * 100:.0f}% 并且逐根走低。放缓≠反转。")))
            mode, reported_slow = "slowing", True
    elif mode == "slowing" and slope <= p["flat_pct"] and not reported_flat:
        turn = "已转负，注明拐头。" if slope < 0 else ""
        events.append(dict(kind="flat", text=(
            f"5 分钟 MA7 斜率接近水平：{slope:.3f}%/根，已落到 +{p['flat_pct']:.2f}%/根以内。{turn}放缓≠反转。")))
        mode, reported_flat = "flat", True
    return dict(mode=mode, peak=peak, prev=slope, reported_slow=reported_slow, reported_flat=reported_flat), events


def quadrant(dp, doi):
    if dp > 0 and doi > 0:
        return "价涨 + 持仓增：新多"
    if dp < 0 and doi > 0:
        return "价跌 + 持仓增：新空"
    if dp > 0 and doi < 0:
        return "价涨 + 持仓减：空头回补"
    if dp < 0 and doi < 0:
        return "价跌 + 持仓减：多头投降"
    return "这一小时价格基本走平，只看持仓变化"


def liq_hit(long_usd, short_usd, spec):
    p = spec["position"]
    total = long_usd + short_usd
    if total < p["liq_usd"] or total <= 0:
        return None
    if long_usd / total >= p["liq_side_frac"]:
        return dict(total=total, side="多单", frac=long_usd / total)
    if short_usd / total >= p["liq_side_frac"]:
        return dict(total=total, side="空单", frac=short_usd / total)
    return None
