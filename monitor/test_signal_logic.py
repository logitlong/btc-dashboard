"""信号规则的离线检查：不联网。面板用同一组夹具再对一遍（test_signal_parity.js）。"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import signal_logic as S

SPEC = S.load_spec()


def osc(rsi, j):
    return {"rsi": rsi, "j": j}


def pack(**kw):
    base = {k: osc(50, 50) for k in ("5m", "15m", "30m", "1h", "4h")}
    base.update(kw)
    return base


def test_tier_wording():
    # 5 分严格、15 分只到配对档：第 2 档，不能写成提示做空
    t = S.resonance(pack(**{"5m": osc(85, 50), "15m": osc(75, 50)}), False, False, SPEC)
    assert t["short"]["tier"] == 2 and t["short"]["via"] == ["RSI"]
    label = S.tier_label("short", 2, t["short"]["via"])
    assert label.startswith("同指标配对") and "提示做空：" not in label and "只是配对" in label
    # 两边 RSI 都过 80 才是提示做空
    t = S.resonance(pack(**{"5m": osc(85, 50), "15m": osc(81, 50)}), False, False, SPEC)
    assert t["short"]["tier"] == 3
    assert S.tier_label("short", 3, t["short"]["via"]).startswith("提示做空")
    # 跨指标拼在一起不算配对
    t = S.resonance(pack(**{"5m": osc(85, 50), "15m": osc(50, 120)}), False, False, SPEC)
    assert t["short"]["tier"] == 1 and t["short"]["funnel"] == 1
    # 只有 J 在两个周期的严格档：提示做空，RSI 优先时两个都有则 RSI 排前面
    t = S.resonance(pack(**{"5m": osc(50, 120), "15m": osc(50, 115)}), False, False, SPEC)
    assert t["short"]["tier"] == 3 and t["short"]["via"] == ["KDJ"]
    t = S.resonance(pack(**{"5m": osc(85, 120), "15m": osc(82, 115)}), False, False, SPEC)
    assert t["short"]["via"][0] == "RSI" and t["short"]["tier"] == 3
    # 做多：两边 J 都小于 -5
    t = S.resonance(pack(**{"5m": osc(40, -6), "15m": osc(40, -8)}), False, False, SPEC)
    assert t["long"]["tier"] == 3 and S.tier_subject("long", 3) == "提示做多"
    # 15 分 J 只小于 0、不到 -5：配对观察，不是提示做多
    t = S.resonance(pack(**{"5m": osc(40, -6), "15m": osc(40, -1)}), False, False, SPEC)
    assert t["long"]["tier"] == 2 and t["long"]["funnel"] == 2
    # 严格背离把第 3 档升成强烈提示
    t = S.resonance(pack(**{"5m": osc(85, 40), "15m": osc(85, 40)}), True, False, SPEC)
    assert t["short"]["tier"] == 4 and S.tier_subject("short", 4).startswith("强烈提示")


def test_extreme_without_requiring_4h():
    hot = dict(rsi=85, j=40)
    mid = dict(rsi=75, j=40)   # 15 分 / 30 分 / 1 小时用各自边界（这里是 70），不到第 3 档的 80
    cool = dict(rsi=55, j=40)
    t = S.resonance({"5m": hot, "15m": mid, "30m": mid, "1h": mid, "4h": cool}, False, False, SPEC)
    assert t["short"]["extreme"] == 5 and t["short"]["tier"] == 5
    assert S.tier_subject("short", 5) == "大仓位做空信号"
    assert "提示做空" not in S.tier_subject("short", 5)
    t = S.resonance({"5m": hot, "15m": mid, "30m": mid, "1h": mid, "4h": mid}, False, False, SPEC)
    assert t["short"]["tier"] == 6 and S.tier_subject("short", 6) == "大仓位做空信号再升一级"
    # 指标不一样不能凑成极端
    t = S.resonance({"5m": hot, "15m": dict(rsi=50, j=130), "30m": mid, "1h": mid, "4h": mid}, False, False, SPEC)
    assert t["short"]["extreme"] == 0
    low = dict(rsi=15, j=-8)
    soft = dict(rsi=18, j=-1)
    t = S.resonance({"5m": low, "15m": soft, "30m": soft, "1h": soft, "4h": dict(rsi=40, j=10)}, False, False, SPEC)
    assert t["long"]["tier"] == 5 and S.tier_subject("long", 5) == "大仓位买入信号"


def test_divergence_uses_median_of_48():
    bars = []
    for i in range(47):
        phase = i % 6
        px = 100 + (phase if phase <= 3 else 6 - phase)
        # 锯齿的高点（phase==3）量是 100，其余是 15。高点量的中位数是 100，不是窗口最大量
        bars.append(dict(t=i, o=px, h=px + 0.5, l=px - 0.5, c=px, v=100 if phase == 3 else 15))
    hi = dict(t=47, o=110, h=120, l=108, c=112, v=80)     # 80 < 100 的 90%
    assert S._div_side(bars + [hi], "high", 48, 0.9)
    hi2 = dict(t=47, o=110, h=120, l=108, c=112, v=95)    # 低于最高点的量，但不低于中位数的 90%
    assert not S._div_side(bars + [hi2], "high", 48, 0.9)
    bars[3]["h"] = 130
    assert not S._div_side(bars + [hi], "high", 48, 0.9)  # 窗口里已经有更高的高点，不是真新高


def _series(n, px, vol=10, spread=1):
    return [dict(t=i * 60000, o=px, h=px + spread, l=px - spread, c=px, v=vol) for i in range(n)]


def test_direction_and_stage():
    # 日线抬升、4 小时回落：冲突时日线是偏多
    daily = _series(120, 100)
    for i, b in enumerate(daily):
        b["c"] = b["o"] = 100 + i
        b["h"], b["l"] = b["c"] + 1, b["c"] - 1
    h4 = _series(120, 100)
    for i, b in enumerate(h4):
        b["c"] = b["o"] = 200 - i * 0.4
        b["h"], b["l"] = b["c"] + 1, b["c"] - 1
    d = S._bias([b["c"] for b in daily], 99, 7)
    h = S._bias([b["c"] for b in h4], 99, 6)
    assert d["dir"] > 0 and h["dir"] < 0
    assert d["bias"] == "偏多" and h["bias"] == "偏空"
    # 4 小时回看是 24 小时 = 6 根，不是 3 根
    assert SPEC["h4"]["lookback"] == 6 and SPEC["daily"]["lookback"] == 7
    up = _series(40, 100)
    for i, b in enumerate(up):
        b["c"] = 100 + i
        b["h"], b["l"], b["o"] = b["c"] + 1, b["c"] - 1, b["c"]
    assert S._stage(up, 25)["name"] == "上涨"
    mixed = _series(40, 100)
    for i, b in enumerate(mixed):
        b["c"] = 100 + (10 if i > 30 else i)   # 价格还在均线上方，但末端走平后均线仍向上时另造一条向下的
    # 价格在均线下方、均线还在升：过渡震荡
    down_px = _series(40, 100)
    for i, b in enumerate(down_px):
        b["c"] = 100 + i
        b["o"], b["h"], b["l"] = b["c"], b["c"] + 1, b["c"] - 1
    down_px[-1]["c"] = 120   # 仍低于此刻 MA25，但这根替换掉更早的低价，均线继续向上
    assert S._stage(down_px, 25)["name"] == "过渡震荡"


def test_three_bull_and_candle():
    def bar(o, h, l, c, v=10):
        return dict(t=0, o=o, h=h, l=l, c=c, v=v)
    # 实体占全幅一半，收盘抬高，回踩不超过前一根全幅的一半，合计超过 0.30%
    a = bar(100, 101, 99.5, 100.8)
    b = bar(100.8, 102, 100.4, 101.6)
    c = bar(101.6, 103, 101.3, 102.6)
    pre = _series(10, 100)
    assert S._three_bull(pre + [a, b, c], SPEC)
    # 合计不够 0.30% 就不算
    small = bar(100, 100.2, 99.9, 100.15)
    assert not S._three_bull(pre + [small, small, small], SPEC)
    # 没有三连阴这一项
    assert not hasattr(S, "_three_bear")
    # 顶部十字长上影
    top = _series(8, 100, spread=0.4)
    star = bar(110, 112, 109.8, 110.05)
    assert S._candle(top + [star], SPEC, True)
    fat = bar(110, 112, 109, 111.5)   # 实体太大，不是十字
    assert not S._candle(top + [fat], SPEC, True)


def test_slope_machine():
    st, ev = S.step_ma7(None, 0.05, SPEC)
    assert st["mode"] == "idle" and not ev
    st, ev = S.step_ma7(st, 0.12, SPEC)
    assert st["mode"] == "surge" and not ev and abs(st["peak"] - 0.12) < 1e-9
    st, ev = S.step_ma7(st, 0.15, SPEC)
    assert not ev and abs(st["peak"] - 0.15) < 1e-9
    # 低于峰值 60%（0.09）且比上一根低
    st, ev = S.step_ma7(st, 0.08, SPEC)
    assert ev and ev[0]["kind"] == "slow" and "放缓≠反转" in ev[0]["text"] and "拐头" not in ev[0]["text"]
    # 还在放缓里，不重复
    st, ev = S.step_ma7(st, 0.07, SPEC)
    assert not ev
    st, ev = S.step_ma7(st, -0.01, SPEC)
    assert ev and ev[0]["kind"] == "flat" and "拐头" in ev[0]["text"] and "放缓≠反转" in ev[0]["text"]
    # 再冲过 +0.10 是新的一波，峰值重置，可以再报放缓
    st, ev = S.step_ma7(st, 0.11, SPEC)
    assert st["mode"] == "surge" and abs(st["peak"] - 0.11) < 1e-9 and not ev
    st, ev = S.step_ma7(st, 0.05, SPEC)
    assert ev and ev[0]["kind"] == "slow"


def test_position_and_email_and_minute():
    assert S.quadrant(0.4, 1.6) == "价涨 + 持仓增：新多"
    assert S.quadrant(-0.2, 2) == "价跌 + 持仓增：新空"
    assert S.quadrant(0.2, -1.6) == "价涨 + 持仓减：空头回补"
    assert S.quadrant(-0.3, -1.8) == "价跌 + 持仓减：多头投降"
    assert S.liq_hit(12e6, 4e6, SPEC)["side"] == "多单"
    assert S.liq_hit(8e6, 8e6, SPEC) is None          # 单边没有到 70%
    assert S.liq_hit(11e6, 1e6, SPEC) is None          # 没到 1500 万
    assert S.liq_hit(20e6, 2e6, SPEC)["side"] == "多单"
    assert SPEC["position"]["oi_1h_pct"] == 1.5
    assert SPEC["position"]["funding_abs"] == 0.03
    assert SPEC["position"]["ls_1h_abs"] == 0.1
    assert SPEC["position_cooldown_sec"] == 7200
    assert SPEC["five_min_every_sec"] == 180 and SPEC["anomaly_every_sec"] == 60
    assert SPEC["position_every_sec"] == 900
    for kind in ("共振", "极端", "盘面异动", "仓位异动"):
        assert S.is_signal_email(kind, "red", SPEC)
    for kind in ("新闻", "事件日历", "跨市场", "背景", "反常异动", "持仓", "盘面异常"):
        assert not S.is_signal_email(kind, "red", SPEC)
    assert not S.is_signal_email("共振", "gray", SPEC)
    vols = [10] * 120
    assert S.minute_spike(vols, [100], dict(c=100.2, v=70), SPEC)["vol"]
    assert not S.minute_spike(vols, [100], dict(c=100.2, v=70), SPEC)["move"]
    assert S.minute_spike(vols, [100], dict(c=100.6, v=10), SPEC)["move"]
    assert not S.minute_spike(vols, [100], dict(c=100.6, v=10), SPEC)["vol"]
    # 用的是前 120 根的中位数，不是 24 小时，也不是 8 倍
    assert SPEC["anomaly"]["vol_mult"] == 6 and SPEC["anomaly"]["vol_median_bars"] == 120
    assert SPEC["anomaly"]["move_pct"] == 0.5


def test_subject_has_both_layers():
    z = {"daily": {"bias": "偏多", "dir": 1, "flat": False, "slope": 943.7, "pct": 1.25, "ma": 1, "price": 1},
         "h4": {"bias": "偏空", "dir": -1, "flat": False, "slope": -266.3, "pct": -0.33, "ma": 1, "price": 1}}
    s = S.direction_subject(z)
    assert "日线MA99偏多" in s and "4小时MA99偏空" in s
    phrase = S.direction_phrase(z)
    assert "以日线为准" in phrase


def dump_fixture():
    """给面板的对照：同一组 K 线，两边的布尔值必须一致。"""
    bars = _series(160, 100, vol=20, spread=0.5)
    for i, b in enumerate(bars):
        b["c"] = 100 + i * 0.05
        b["o"] = b["c"] - 0.02
        b["h"] = b["c"] + 0.4
        b["l"] = b["c"] - 0.3
        b["v"] = 30 if i % 7 == 0 else 12
    # 最后一根真新高但量很小
    bars[-1]["h"] = max(b["h"] for b in bars) + 2
    bars[-1]["c"] = bars[-1]["h"] - 0.1
    bars[-1]["o"] = bars[-1]["c"] - 0.05
    bars[-1]["v"] = 3
    daily = _series(120, 100)
    for i, b in enumerate(daily):
        b["c"] = b["o"] = 100 + i * 0.8
        b["h"], b["l"] = b["c"] + 1, b["c"] - 1
    h4 = _series(120, 100)
    for i, b in enumerate(h4):
        b["c"] = b["o"] = 300 - i * 0.5
        b["h"], b["l"] = b["c"] + 1, b["c"] - 1
    bundle = {"5m": bars, "15m": bars[-80:], "30m": bars[-80:], "1h": bars[-80:], "4h": h4, "1d": daily}
    z = S.evaluate(bundle, SPEC, now_ms=bars[-1]["t"] + 60000)
    keep = dict(div_s=z["div_s"], div_l=z["div_l"], tier=z["tier"], tier_side=z["tier_side"],
                six_s=[x["on"] for x in z["six_s"]], six_l=[x["on"] for x in z["six_l"]],
                custom=[x["on"] for x in z["custom"]], three_bull=z["three_bull"],
                daily_dir=z["daily"]["dir"], h4_dir=z["h4"]["dir"],
                stage1h=z["stage1h"]["name"], stage30=z["stage30"]["name"],
                label=z["label"], ma7_slope=round(z["ma7_slope"], 6))
    path = os.path.join(os.path.dirname(__file__), "fixtures")
    os.makedirs(path, exist_ok=True)
    json.dump(dict(spec_keys=dict(bounds=SPEC["bounds"], six=SPEC["six"], candle=SPEC["candle"],
                                  custom=SPEC["custom"], anomaly=SPEC["anomaly"],
                                  daily=SPEC["daily"], h4=SPEC["h4"], mid_ma=SPEC["mid_ma"]),
                   bundle=bundle, now_ms=bars[-1]["t"] + 60000, expect=keep),
              open(os.path.join(path, "signal_case.json"), "w", encoding="utf-8"), ensure_ascii=False)
    return keep


if __name__ == "__main__":
    test_tier_wording()
    test_extreme_without_requiring_4h()
    test_divergence_uses_median_of_48()
    test_direction_and_stage()
    test_three_bull_and_candle()
    test_slope_machine()
    test_position_and_email_and_minute()
    test_subject_has_both_layers()
    print("fixture", dump_fixture())
    print("ok")
