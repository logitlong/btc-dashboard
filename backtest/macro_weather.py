"""大环境水温回测：QQQ / GLD / BTC 相对 50 日均线、避险背离，对之后 BTC 的涨跌和波动有没有用。

数据：data/macro/QQQ.json, GLD.json（Yahoo 日线），data/BTCUSD_bitstamp_1h.csv。
时间对齐（没有未来数据）：用美股第 t 天收盘后的下一个 UTC 0 点的 BTC 价格作为“看到信号后”的起点，
QQQ 收盘在 UTC 20/21 点，0 点之前已经知道。往后看 1 天、7 天的收益，以及 7 天内日收益绝对值的均值（波动）。
样本内 2015-2021，样本外 2022 以后。输出到 results/macro_weather.json，面板直接读其中的数字。

用法：backtest/.venv/bin/python backtest/macro_weather.py
"""
import json
from pathlib import Path

import numpy as np
import pandas as pd

D = Path(__file__).parent / "data"
OUT = Path(__file__).parent / "results" / "macro_weather.json"
SPLIT = "2022-01-01"


def yahoo(sym):
    r = json.load(open(D / "macro" / f"{sym}.json"))["chart"]["result"][0]
    s = pd.Series(r["indicators"]["adjclose"][0]["adjclose"], index=pd.to_datetime(r["timestamp"], unit="s").normalize(), name=sym)
    return s.dropna()


def main():
    q, g = yahoo("QQQ"), yahoo("GLD")
    b = pd.read_csv(D / "BTCUSD_bitstamp_1h.csv", usecols=["open_time", "close"])
    b.index = pd.to_datetime(b.open_time, unit="ms")
    bd = b.close.resample("1D").last().dropna()  # 当天 UTC 收盘 = 次日 0 点价格
    sp = pd.read_csv(D / "BTCUSDT_spot_1m.csv.gz", usecols=[0, 4])
    sp.index = pd.to_datetime(sp.iloc[:, 0], unit="ms")
    sd = sp.iloc[:, 1].resample("1D").last().dropna()
    bd = pd.concat([bd[bd.index < sd.index[0]], sd])  # 2017-08 以前用 Bitstamp，以后用币安现货
    bd = bd[bd.index >= "2014-06-01"]

    m = pd.DataFrame({"q": q, "g": g}).dropna()
    m["qdev"] = m.q / m.q.rolling(50).mean() - 1
    m["gdev"] = m.g / m.g.rolling(50).mean() - 1
    m["flight"] = m.g.pct_change(20) - m.q.pct_change(20)  # 避险(金) 20 天跑赢科技(纳指)多少
    # BTC：日 t 的 UTC 收盘（=t+1 的 0 点）就是起点，和 QQQ t 日收盘之后对齐
    bdev = bd / bd.rolling(50).mean() - 1
    lr = np.log(bd).diff()
    f1 = np.log(bd.shift(-1) / bd)
    f7 = np.log(bd.shift(-7) / bd)
    v7 = lr.abs()[::-1].rolling(7).mean()[::-1].shift(-1)  # 之后 7 天日收益绝对值均值
    vb = lr.abs().rolling(30).mean()  # 过去 30 天的，用来比
    m = m.join(pd.DataFrame({"bdev": bdev, "f1": f1, "f7": f7, "vr": v7 / vb}), how="inner").dropna()
    m = m[m.index >= "2015-01-01"]

    def state(r):
        if r.qdev < -0.03 and r.bdev < -0.05:
            return "storm"  # 纳指和 BTC 都明显在 50 日线下
        if r.qdev > 0 and r.bdev > 0:
            return "sun"
        if r.qdev < 0 and r.bdev < 0:
            return "rain"
        return "mixed"

    m["st"] = m.apply(state, axis=1)
    m["fl"] = np.where(m.flight > 0.05, "flight", np.where(m.flight < -0.05, "riskon", "flat"))

    def stats(df):
        n = len(df)
        if n < 5:
            return {"n": n}
        f7 = df.f7
        # 7 天收益重叠，用 n/7 作为有效样本
        t = f7.mean() / (f7.std() / np.sqrt(max(n / 7, 1)))
        return {"n": n, "f1": round(df.f1.mean() * 100, 3), "f7": round(f7.mean() * 100, 2), "up7": round((f7 > 0).mean() * 100, 1),
                "t7": round(float(t), 2), "vr": round(df.vr.median(), 2)}

    res = {"split": SPLIT, "span": [str(m.index[0].date()), str(m.index[-1].date())], "base": {}, "state": {}, "flight": {}, "years": {}}
    for nm, part in (("is", m[m.index < SPLIT]), ("oos", m[m.index >= SPLIT]), ("all", m)):
        res["base"][nm] = stats(part)
        res["state"][nm] = {k: stats(part[part.st == k]) for k in ("sun", "mixed", "rain", "storm")}
        res["flight"][nm] = {k: stats(part[part.fl == k]) for k in ("flight", "flat", "riskon")}
    for y, part in m.groupby(m.index.year):
        res["years"][int(y)] = {k: stats(part[part.st == k]).get("f7") for k in ("sun", "storm")} | {"vr_storm": stats(part[part.st == "storm"]).get("vr")}
    # 相关：BTC 和 QQQ 日收益 60 天滚动相关的最近值，以及历史分位
    j = pd.DataFrame({"b": lr, "q": np.log(q).diff()}).dropna()
    c = j.b.rolling(60).corr(j.q).dropna()
    res["corr60_pct"] = {str(p): round(float(c.quantile(p / 100)), 2) for p in (10, 50, 90)}
    OUT.write_text(json.dumps(res, ensure_ascii=False, indent=1))
    print(json.dumps(res, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
