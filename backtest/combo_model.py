"""合成模型：把盘口、主动买卖、多空比、持仓、期权、价格本身的因子放在一起，权重从历史里学。

数据和 alt_data_study.py 同一套（先跑 fetch_alt_data.py）。每个整点一行，所有因子都只用那一刻已经能看到的值。
目标：之后 1 小时 / 4 小时的对数收益（币安永续）。

三种配权重的方法，都走步：用过去 180 天训练，测之后 7 天，再往前推；训练和测试之间空出一个持有周期。
1. IC 加权：每个因子按训练期的秩相关 IC 给权重（最朴素、最不容易过拟合）
2. 岭回归：线性回归加惩罚，和波动尺子同一种
3. 梯度提升树：能学「A 高并且 B 低」这种组合，最容易过拟合

下单：样本外预测的绝对值超过「训练期预测绝对值的 90% / 95% 分位」才开仓，持有到期，不叠仓。
手续费两种：吃单往返 0.10%；挂单往返 0.04%（挂单假设都能成交，偏乐观，挂单会被逆向选择）。
所有组合一起做 Deflated Sharpe 和 PBO（overfit.py）。

用法：.venv/bin/python combo_model.py   输出写到 results_combo.txt
"""
import glob
import json
import os
import sys
import warnings

import numpy as np
import pandas as pd

warnings.filterwarnings("ignore")
np.seterr(all="ignore")
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import alt_data_study as A  # noqa: E402
import overfit  # noqa: E402

HIST = A.HIST
START = pd.Timestamp("2023-01-08", tz="UTC")  # 盘口从 2023-01 起
TRAIN_DAYS, STEP_DAYS = 180, 7
TEST_FROM = pd.Timestamp("2024-01-01", tz="UTC")  # 只报告 2024 年以后（前一年用来攒训练）
FEES = {"吃单 0.10%": 0.10, "挂单 0.04%": 0.04}
QUANTS = (0.90, 0.95)
OUT = []


def say(*a):
    s = " ".join(str(x) for x in a)
    print(s, flush=True)
    OUT.append(s)


def last_at(series, times):
    """每个时刻能看到的最后一个值（series 按「能看到的时刻」标）。"""
    s = series.dropna().sort_index()
    s = s[~s.index.duplicated(keep="last")]
    return s.reindex(times, method="ffill", tolerance=pd.Timedelta(hours=6)).to_numpy()


def option_features():
    cache = os.path.join(HIST, "opt_feats.pkl")
    if os.path.exists(cache):
        return pd.read_pickle(cache)
    rows = {}
    for f in sorted(glob.glob(os.path.join(HIST, "deribit_opt", "*.json"))):
        t_end = int(os.path.basename(f)[:-5])
        snap = A.option_snapshot(json.load(open(f)), t_end)
        if snap:
            rows[pd.Timestamp(t_end, unit="s", tz="UTC")] = snap
    o = pd.DataFrame(rows).T.sort_index().astype(float)
    o.to_pickle(cache)
    return o


def build_frame():
    price = A.load_perp()
    close = price.close
    times = pd.date_range(START, close.index[-1] - pd.Timedelta(hours=4), freq="1h", tz="UTC")
    f = pd.DataFrame(index=times)
    lp = np.log(close)
    at = lambda s: last_at(s, times)  # noqa: E731
    # 价格本身
    for h in (1, 4, 24):
        f[f"ret_{h}h"] = at(lp.diff(12 * h))
    rng = np.log(price.high / price.low)
    f["vol_24h"] = at(np.log(rng.rolling(288).mean()))
    # 盘口
    b = pd.concat([pd.read_csv(x) for x in sorted(glob.glob(os.path.join(HIST, "bookdepth", "*.csv"))) if os.path.getsize(x) > 20])
    b["t"] = pd.to_datetime(b.t, utc=True) + pd.Timedelta(minutes=5)
    w = b.pivot_table(index="t", columns="pct", values="notional", aggfunc="last").sort_index()
    w.columns = [float(c) for c in w.columns]
    imb = lambda a: (w[-a] - w[a]) / (w[-a] + w[a])  # noqa: E731
    f["imb_02"], f["imb_1"], f["imb_5"] = at(imb(0.2)), at(imb(1.0)), at(imb(5.0))
    f["imb_1_avg1h"] = at(imb(1.0).rolling(12).mean())
    dep = np.log(w[-1.0] + w[1.0])
    f["depth_1"] = at(dep - dep.rolling("7D").median())
    # 持仓、多空比、主动买卖
    m = pd.concat([pd.read_csv(x) for x in sorted(glob.glob(os.path.join(HIST, "metrics", "*.csv"))) if os.path.getsize(x) > 0])
    m["t"] = pd.to_datetime(m.create_time, utc=True).dt.floor("5min") + pd.Timedelta(minutes=5)
    m = m.drop_duplicates("t").set_index("t").sort_index()
    for c in ("sum_open_interest", "sum_toptrader_long_short_ratio", "count_long_short_ratio", "sum_taker_long_short_vol_ratio"):
        m[c] = pd.to_numeric(m[c], errors="coerce")
    m = m.resample("5min").last().ffill(limit=2)
    oi = np.log(m.sum_open_interest.where(m.sum_open_interest > 0))
    f["oi_1h"], f["oi_24h"] = at(oi.diff(12)), at(oi.diff(288))
    f["ls_all"] = at(np.log(m.count_long_short_ratio))
    f["ls_top_minus_all"] = at(np.log(m.sum_toptrader_long_short_ratio) - np.log(m.count_long_short_ratio))
    f["taker_1h"] = at(np.log(m.sum_taker_long_short_vol_ratio).rolling(12).mean())
    # 期权和 DVOL
    o = option_features()
    f["atm_iv"], f["skew"], f["opt_flow"] = at(o["atm_iv"]), at(o["skew"]), at(o["flow"])
    dv = json.load(open(os.path.join(HIST, "dvol.json")))
    dvol = pd.Series([r[4] for r in dv], index=pd.to_datetime([r[0] for r in dv], unit="ms", utc=True) + pd.Timedelta(hours=1))
    f["dvol_24h"] = at(np.log(dvol).diff(24))
    # 目标
    for h in (1, 4):
        f[f"y_{h}h"] = A.fwd_ret(close, times, 60 * h).to_numpy()
    return f.replace([np.inf, -np.inf], np.nan)


FEATS = ["ret_1h", "ret_4h", "ret_24h", "vol_24h", "imb_02", "imb_1", "imb_5", "imb_1_avg1h", "depth_1",
         "oi_1h", "oi_24h", "ls_all", "ls_top_minus_all", "taker_1h", "atm_iv", "skew", "opt_flow", "dvol_24h"]


def standardize(tr, te):
    mu, sd = tr.mean(), tr.std().replace(0, 1)
    return ((tr - mu) / sd).clip(-5, 5).fillna(0), ((te - mu) / sd).clip(-5, 5).fillna(0)


def fit_predict(kind, xtr, ytr, xte):
    if kind == "IC 加权":
        ic = xtr.rank().corrwith(ytr.rank()).fillna(0)
        return (xtr @ ic).to_numpy(), (xte @ ic).to_numpy()
    if kind == "岭回归":
        from sklearn.linear_model import Ridge
        mdl = Ridge(alpha=len(xtr) * 0.01).fit(xtr, ytr)
        return mdl.predict(xtr), mdl.predict(xte)
    from sklearn.ensemble import HistGradientBoostingRegressor
    mdl = HistGradientBoostingRegressor(max_depth=3, max_iter=150, learning_rate=0.05, min_samples_leaf=200,
                                        l2_regularization=1.0, random_state=0).fit(xtr, ytr)
    return mdl.predict(xtr), mdl.predict(xte)


def walk(frame, h, kind):
    y = f"y_{h}h"
    d = frame.dropna(subset=[y])
    t = START + pd.Timedelta(days=TRAIN_DAYS)
    parts = []
    while t <= d.index[-1]:
        tr = d[(d.index < t - pd.Timedelta(hours=h)) & (d.index >= t - pd.Timedelta(days=TRAIN_DAYS))]
        te = d[(d.index >= t) & (d.index < t + pd.Timedelta(days=STEP_DAYS))]
        if len(te) and len(tr) > 24 * 60:
            xtr, xte = standardize(tr[FEATS], te[FEATS])
            ptr, pte = fit_predict(kind, xtr, tr[y], xte)
            thr = {q: float(np.quantile(np.abs(ptr), q)) for q in QUANTS}
            parts.append(pd.DataFrame({"pred": pte, "y": te[y].to_numpy(),
                                       **{f"thr_{q}": thr[q] for q in QUANTS}}, index=te.index))
        t += pd.Timedelta(days=STEP_DAYS)
    return pd.concat(parts)


def trades(oos, h, q):
    """超过门槛就开仓，持有 h 小时，期间不再开。返回单笔毛收益（%），按开仓时间标。"""
    out, busy_until = {}, None
    for ts, row in oos.iterrows():
        if busy_until is not None and ts < busy_until:
            continue
        if abs(row.pred) > row[f"thr_{q}"] and np.isfinite(row.y):
            out[ts] = np.sign(row.pred) * row.y
            busy_until = ts + pd.Timedelta(hours=h)
    return pd.Series(out, dtype=float)


def main():
    cache = os.path.join(HIST, "combo_frame.pkl")
    if os.path.exists(cache):
        frame = pd.read_pickle(cache)
    else:
        frame = build_frame()
        frame.to_pickle(cache)
    say(f"合成模型：{len(FEATS)} 个因子，{frame.index[0]:%Y-%m-%d} – {frame.index[-1]:%Y-%m-%d}，每小时一行")
    say(f"走步：训练 {TRAIN_DAYS} 天，测 {STEP_DAYS} 天，训练和测试之间空出一个持有周期；只报告 {TEST_FROM:%Y-%m-%d} 以后")
    trials = []
    for h in (1, 4):
        say(f"\n======== 预测之后 {h} 小时")
        for kind in ("IC 加权", "岭回归", "梯度提升树"):
            oos = walk(frame, h, kind)
            oos = oos[oos.index >= TEST_FROM]
            ic = oos.pred.rank().corr(oos.y.rank())
            by = oos.groupby(oos.index.year).apply(lambda g: g.pred.rank().corr(g.y.rank()))
            hit = (np.sign(oos.pred) == np.sign(oos.y)).mean()
            say(f"\n### {kind}：样本外 IC {ic:+.3f}，方向猜对 {hit * 100:.1f}%（{len(oos)} 小时）| 按年 IC "
                + " ".join(f"{y % 100:02d}:{v:+.3f}" for y, v in by.items()))
            for q in QUANTS:
                g = trades(oos, h, q)
                if g.empty:
                    continue
                cells = []
                for fname, fee in FEES.items():
                    net = g - fee
                    yr = net.groupby(net.index.year).mean()
                    cells.append(f"{fname} {net.mean():+.3f}%/笔（" + " ".join(f"{y % 100:02d}:{v:+.3f}" for y, v in yr.items()) + "）")
                    trials.append({"name": f"{kind}·前{(1 - q) * 100:.0f}%·{fname}", "hz": f"{h}h",
                                   "ic": ic, "n": len(oos), "pnl": net})
                say(f"  只做最强的 {(1 - q) * 100:.0f}%：{len(g)} 笔，猜对 {(g > 0).mean() * 100:.1f}%，毛 {g.mean():+.3f}%/笔 | "
                    + " | ".join(cells))
            if kind == "岭回归":
                xs, _ = standardize(frame[FEATS].dropna(how="all"), frame[FEATS].iloc[:1])
                from sklearn.linear_model import Ridge
                yy = frame.loc[xs.index, f"y_{h}h"]
                ok = yy.notna()
                coef = Ridge(alpha=ok.sum() * 0.01).fit(xs[ok], yy[ok]).coef_
                top = sorted(zip(FEATS, coef), key=lambda x: -abs(x[1]))[:6]
                say("  全样本岭回归权重（只为看哪些因子在起作用，不用于上面的样本外结果）："
                    + "，".join(f"{k} {v * 100:+.4f}" for k, v in top))
    overfit.report(trials, say, fee="按各自写明的吃单 / 挂单")
    open(os.path.join(HERE, "results_combo.txt"), "w").write("\n".join(OUT) + "\n")


if __name__ == "__main__":
    main()
