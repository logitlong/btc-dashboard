"""K 线以外的数据能不能预测 BTC：持仓量 / 多空比、盘口深度、期权。

先跑 fetch_alt_data.py 下载（metrics、bookdepth、deribit），DVOL 用 data/history/dvol.json。
价格用币安永续 1 分钟（data/BTCUSDT_perp_1m.csv.gz）。

时间对齐：每个数都按「什么时候能看到」标时间。币安 metrics 和盘口按 5 分钟桶再往后挪 5 分钟，
Deribit 期权用整点前 1 小时的成交，整点才算看到。之后的涨跌从看到那一刻的价格算起。

每个因子做两件事：
1. 方向：和之后 15 分钟 / 1 小时 / 4 小时 / 24 小时涨跌的秩相关（IC），样本不重叠，按年分开。
   简单打法：因子 z 分数（只用过去 7 天）超过 ±1.5 就按 2021–2023 年 IC 的方向开仓，持有到期，
   扣往返 0.10%（吃单两边），只看 2024 年以后（方向是之前定的，算样本外）。
2. 波动：叠到波动尺子（monitor/vol_model.py）的 4 小时振幅模型上，看样本外 R² 多了多少。

因子很多、周期很多，偶然冒出一两个「显著」很正常。只有每年方向一致、扣费后样本外还赚钱的才算数。

用法：.venv/bin/python alt_data_study.py [metrics|book|options|all]   输出写到 results_alt_data.txt
"""
import glob
import json
import math
import os
import sys
import warnings

import numpy as np
import pandas as pd

warnings.filterwarnings("ignore")
np.seterr(all="ignore")
HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "data")
HIST = os.path.join(DATA, "history")
sys.path.insert(0, os.path.join(HERE, "..", "monitor"))
import vol_model as VM  # noqa: E402
import overfit  # noqa: E402

FEE_RT = 0.10  # %，往返吃单
HORIZONS = {"15m": 15, "1h": 60, "4h": 240, "24h": 1440}
OUT = []
TRIALS = []  # 每个「因子 × 周期」一条，最后一起做过拟合校正


def say(*a):
    s = " ".join(str(x) for x in a)
    print(s, flush=True)
    OUT.append(s)


# ---------------- 价格 ----------------
def load_perp():
    cache = os.path.join(HIST, "perp_5m.pkl")
    if os.path.exists(cache):
        return pd.read_pickle(cache)
    d = pd.read_csv(os.path.join(DATA, "BTCUSDT_perp_1m.csv.gz"), usecols=["open_time", "high", "low", "close"])
    d.index = pd.to_datetime(d.open_time, unit="ms", utc=True)
    b = d.resample("5min").agg(high=("high", "max"), low=("low", "min"), close=("close", "last")).dropna()
    b.index = b.index + pd.Timedelta(minutes=5)  # 按收盘时刻标
    b.to_pickle(cache)
    return b


def load_hourly_spot():
    cache = os.path.join(HIST, "spot_1h.pkl")
    if os.path.exists(cache):
        return pd.read_pickle(cache)
    parts = []
    for c in pd.read_csv(os.path.join(DATA, "BTCUSDT_spot_1m.csv.gz"),
                         usecols=["open_time", "open", "high", "low", "close", "volume"], chunksize=2_000_000):
        c = c[c.open_time >= 1577836800000]
        if c.empty:
            continue
        c.index = pd.to_datetime(c.open_time, unit="ms", utc=True)
        parts.append(c.resample("1h").agg(open=("open", "first"), high=("high", "max"), low=("low", "min"),
                                          close=("close", "last"), volume=("volume", "sum")))
    h = pd.concat(parts)
    h = h.groupby(level=0).agg(open=("open", "first"), high=("high", "max"), low=("low", "min"),
                               close=("close", "last"), volume=("volume", "sum")).dropna().iloc[:-1]
    h.to_pickle(cache)
    return h


# ---------------- 检验 ----------------
def zscore(s, days=7):
    """只用过去：窗口按时间 7 天，当前值不进自己的均值。"""
    w = f"{days}D"
    mu = s.shift(1).rolling(w, min_periods=50).mean()
    sd = s.shift(1).rolling(w, min_periods=50).std()
    return (s - mu) / sd


def fwd_ret(price, times, minutes):
    """times 时刻（已经能看到因子）之后 minutes 分钟的对数收益，%。price 按收盘时刻标。"""
    p0 = price.reindex(times, method="ffill", tolerance=pd.Timedelta(minutes=5))
    p1 = price.reindex(times + pd.Timedelta(minutes=minutes), method="ffill", tolerance=pd.Timedelta(minutes=5))
    return pd.Series(np.log(p1.to_numpy() / p0.to_numpy()) * 100, index=times)


def spearman(a, b):
    ok = np.isfinite(a) & np.isfinite(b)
    if ok.sum() < 30:
        return np.nan, int(ok.sum())
    return pd.Series(a[ok]).rank().corr(pd.Series(b[ok]).rank()), int(ok.sum())


def direction_test(name, feat, price, z_feat=True, z_days=7):
    """feat：按「能看到的时刻」标的序列。4 小时一个点的数据 7 天只有 42 个，z 分数窗口要放长。"""
    feat = feat.dropna()
    feat = feat[~feat.index.duplicated(keep="last")].sort_index()
    if z_feat:
        feat = zscore(feat, z_days)
    feat = feat.replace([np.inf, -np.inf], np.nan).dropna()
    rows = []
    for hz, mins in HORIZONS.items():
        step = f"{max(mins, 5)}min"
        f = feat.resample(step, label="right", closed="right").last().dropna()
        r = fwd_ret(price, f.index, mins)
        years = sorted(set(f.index.year))
        ics = {}
        for y in years:
            m = f.index.year == y
            ic, n = spearman(f[m].to_numpy(), r[m].to_numpy())
            ics[y] = (ic, n)
        all_ic, n_all = spearman(f.to_numpy(), r.to_numpy())
        # 打法：方向由 2024 年以前定，2024 年以后算账
        train = f.index.year < 2024
        ic_tr, _ = spearman(f[train].to_numpy(), r[train].to_numpy())
        sgn = np.sign(ic_tr) if np.isfinite(ic_tr) else 0
        test = ~train
        sig = np.where(f[test] > 1.5, 1, np.where(f[test] < -1.5, -1, 0)) * sgn
        pnl = pd.Series(sig * r[test].to_numpy(), index=f.index[test])[sig != 0].dropna()
        tr = pnl.to_numpy()
        net = tr - FEE_RT
        by_year = (pnl - FEE_RT).groupby(pnl.index.year).mean().to_dict()
        TRIALS.append({"name": name, "hz": hz, "ic": all_ic, "n": n_all, "pnl": pnl - FEE_RT})
        rows.append((hz, all_ic, n_all, ics, sgn, len(tr), float(np.mean(tr)) if len(tr) else np.nan,
                     float(np.mean(net)) if len(net) else np.nan, by_year))
    say(f"\n### {name}")
    for hz, all_ic, n_all, ics, sgn, nt, gross, net, by_year in rows:
        t = all_ic * math.sqrt(max(n_all - 2, 1)) if np.isfinite(all_ic) else np.nan
        yrs = " ".join(f"{y % 100:02d}:{ic:+.3f}" for y, (ic, _) in ics.items() if np.isfinite(ic))
        same = sum(1 for ic, _ in ics.values() if np.isfinite(ic) and np.sign(ic) == np.sign(all_ic))
        tot = sum(1 for ic, _ in ics.values() if np.isfinite(ic))
        by = " ".join(f"{y % 100:02d}:{v:+.3f}" for y, v in by_year.items())
        say(f"  {hz:>4} IC {all_ic:+.3f} (t≈{t:+.1f}, n={n_all}) 同号 {same}/{tot} 年 | {yrs}")
        say(f"       打法 2024+（方向 {'+' if sgn > 0 else '-' if sgn < 0 else '0'}）{nt} 笔 毛 {gross:+.3f}% 扣费 {net:+.3f}%/笔 | {by}")
    return rows


def vol_test(name, feats_by_hour, frame_base):
    """feats_by_hour：DataFrame，按小时开盘时间标（这一小时收盘时能看到）。"""
    fr = frame_base.join(feats_by_hour.replace([np.inf, -np.inf], np.nan), how="inner")
    cols = list(feats_by_hour.columns)
    fr = fr.dropna(subset=VM.FULL_FEATURES + cols + ["y4"])
    if len(fr) < 24 * 60:
        say(f"  波动 {name}: 对齐的小时只有 {len(fr)}，不够")
        return
    base, n = oos_r2(fr, VM.FULL_FEATURES)
    plus, _ = oos_r2(fr, VM.FULL_FEATURES + cols)
    say(f"  波动（4 小时振幅，样本外 R²，{fr.index[0]:%Y-%m}–{fr.index[-1]:%Y-%m}, {n} 小时）：原模型 {base:.3f} → 加上 {name} {plus:.3f}（{plus - base:+.3f}）")


def oos_r2(fr, feats, horizon=4):
    """和 VM.oos_r2 同一套走步，只是遇到数据空档跳过那一周，不提前结束。"""
    d = fr[fr.y4 > 0].copy()
    d["log_y"] = np.log(d.y4)
    t, end = d.index[0] + pd.Timedelta(days=VM.TRAIN_DAYS), d.index[-1]
    ys, ps = [], []
    while t <= end:
        tr = d[d.index < t - pd.Timedelta(hours=horizon)]
        te = d[(d.index >= t) & (d.index < t + pd.Timedelta(days=VM.STEP_DAYS))]
        if len(te) and len(tr) >= 24 * 7:
            ys.append(te.log_y.to_numpy())
            ps.append(VM._predict_pack(VM._fit_cols(tr, feats, tr.log_y), te))
        t += pd.Timedelta(days=VM.STEP_DAYS)
    y, p = np.concatenate(ys), np.concatenate(ps)
    return VM.r2_score(y, p), len(y)


def hourly_frame():
    h = load_hourly_spot()
    return VM.features_from_hourly(h)


def at_hour(series_by_avail):
    """能看到的时刻 → 小时开盘时间（这一小时收盘时能看到的最后一个值）。"""
    s = series_by_avail.sort_index()
    s = s[~s.index.duplicated(keep="last")]
    # [h, h+1h) 里看到的最后一个值，这一小时收盘前已经知道；VM 的小时按开盘时间 h 标
    return s.resample("1h", label="left", closed="left").last()


# ---------------- 1. 持仓量 / 多空比 ----------------
def study_metrics(price, hf):
    files = sorted(glob.glob(os.path.join(HIST, "metrics", "*.csv")))
    parts = [pd.read_csv(f) for f in files if os.path.getsize(f) > 0]
    m = pd.concat(parts)
    m["t"] = pd.to_datetime(m.create_time, utc=True).dt.floor("5min") + pd.Timedelta(minutes=5)
    m = m.drop_duplicates("t").set_index("t").sort_index()
    for c in ("sum_open_interest", "count_toptrader_long_short_ratio", "sum_toptrader_long_short_ratio",
              "count_long_short_ratio", "sum_taker_long_short_vol_ratio"):
        m[c] = pd.to_numeric(m[c], errors="coerce")
    m = m.resample("5min").last().ffill(limit=2)
    say(f"\n======== 1. 持仓量 / 多空比（币安 BTCUSDT 永续，{m.index[0]:%Y-%m-%d} – {m.index[-1]:%Y-%m-%d}，{len(m)} 个 5 分钟）")
    oi = np.log(m.sum_open_interest)
    px = price.close.reindex(m.index, method="ffill")
    lp = np.log(px)
    feats = {
        "持仓 1 小时变化": oi.diff(12),
        "持仓 4 小时变化": oi.diff(48),
        "持仓 24 小时变化": oi.diff(288),
        "大户多空比（人数）": m.count_toptrader_long_short_ratio,
        "大户多空比（持仓）": m.sum_toptrader_long_short_ratio,
        "全部账户多空比": m.count_long_short_ratio,
        "全部账户多空比 4 小时变化": np.log(m.count_long_short_ratio).diff(48),
        "大户 − 全部账户（持仓口径）": np.log(m.sum_toptrader_long_short_ratio) - np.log(m.count_long_short_ratio),
        "主动买卖比（1 小时平均）": np.log(m.sum_taker_long_short_vol_ratio).rolling(12).mean(),
    }
    for k, v in feats.items():
        direction_test(k, v, price.close)
    # 面板「仓位结构」四象限：持仓 1 小时变化绝对值 ≥ 1.5%
    say("\n### 面板「仓位结构」四象限（持仓 1 小时变化 ≥ 1.5%，同一小时价格方向），之后 1 / 4 / 24 小时")
    d_oi = oi.diff(12) * 100
    d_p = lp.diff(12) * 100
    ev = pd.DataFrame({"doi": d_oi, "dp": d_p}).dropna()
    ev = ev[ev.doi.abs() >= 1.5]
    ev = ev[ev.index.to_series().diff().fillna(pd.Timedelta(hours=9)) >= pd.Timedelta(hours=2)]  # 冷却 2 小时
    lab = np.where(ev.dp > 0, np.where(ev.doi > 0, "价涨仓增（新多）", "价涨仓减（空头回补）"),
                   np.where(ev.doi > 0, "价跌仓增（新空）", "价跌仓减（多头投降）"))
    for mins, hz in ((60, "1h"), (240, "4h"), (1440, "24h")):
        ev[hz] = fwd_ret(price.close, ev.index, mins).to_numpy()
    ev["lab"] = lab
    allr = {hz: fwd_ret(price.close, price.close.index[price.close.index >= ev.index[0]][::12], mins).dropna()
            for mins, hz in ((60, "1h"), (240, "4h"), (1440, "24h"))}
    say("  对照（同期所有整点）: " + " | ".join(f"{hz} 均值 {v.mean():+.3f}% 涨的比例 {(v > 0).mean() * 100:.0f}%" for hz, v in allr.items()))
    for g, x in ev.groupby("lab"):
        cells = []
        for hz in ("1h", "4h", "24h"):
            v = x[hz].dropna()
            cells.append(f"{hz} 均值 {v.mean():+.3f}% 涨的比例 {(v > 0).mean() * 100:.0f}%")
        yr = x.groupby(x.index.year)["4h"].mean()
        say(f"  {g}: {len(x)} 次 | " + " | ".join(cells) + " | 4h 按年 " + " ".join(f"{y % 100:02d}:{v:+.2f}" for y, v in yr.items()))
    # 波动
    hv = pd.DataFrame({
        "oi_chg_1h": at_hour(oi.diff(12)).abs(),
        "oi_chg_24h": at_hour(oi.diff(288)),
        "ls_all": at_hour(np.log(m.count_long_short_ratio)),
    })
    vol_test("持仓变化和多空比", hv, hf)


# ---------------- 2. 盘口深度 ----------------
def study_book(price, hf):
    files = sorted(glob.glob(os.path.join(HIST, "bookdepth", "*.csv")))
    parts = [pd.read_csv(f) for f in files if os.path.getsize(f) > 20]
    b = pd.concat(parts)
    b["t"] = pd.to_datetime(b.t, utc=True) + pd.Timedelta(minutes=5)
    w = b.pivot_table(index="t", columns="pct", values="notional", aggfunc="last").sort_index()
    w.columns = [float(c) for c in w.columns]
    say(f"\n======== 2. 盘口深度（币安 BTCUSDT 永续，{w.index[0]:%Y-%m-%d} – {w.index[-1]:%Y-%m-%d}，{len(w)} 个 5 分钟）")
    imb = lambda a: (w[-a] - w[a]) / (w[-a] + w[a])  # noqa: E731  买盘多为正
    feats = {
        "盘口失衡 ±0.2%": imb(0.2),
        "盘口失衡 ±1%": imb(1.0),
        "盘口失衡 ±1%（1 小时平均）": imb(1.0).rolling(12).mean(),
        "盘口失衡 ±5%": imb(5.0),
        "±1% 总深度相对 7 天": np.log(w[-1.0] + w[1.0]),
    }
    for k, v in feats.items():
        direction_test(k, v, price.close)
    dep = np.log(w[-1.0] + w[1.0])
    hv = pd.DataFrame({"depth1": at_hour(dep - dep.rolling("7D").median()), "imb1": at_hour(imb(1.0)).abs()})
    vol_test("盘口深度", hv, hf)


# ---------------- 3. 期权 ----------------
def norm_cdf(x):
    # Abramowitz–Stegun 7.1.26，误差约 1e-7，免得为这个装 scipy
    z = np.abs(x) / math.sqrt(2)
    t = 1 / (1 + 0.3275911 * z)
    erf = 1 - (((((1.061405429 * t - 1.453152027) * t) + 1.421413741) * t - 0.284496736) * t + 0.254829592) * t * np.exp(-z * z)
    return 0.5 * (1 + np.sign(x) * erf)


def bs_price(F, K, T, sig, call):
    sq = sig * np.sqrt(T)
    d1 = (np.log(F / K) + 0.5 * sq * sq) / sq
    d2 = d1 - sq
    c = F * norm_cdf(d1) - K * norm_cdf(d2)
    return np.where(call, c, c - F + K)


def implied_vol(px_usd, F, K, T, call):
    lo = np.full(len(F), 0.05)
    hi = np.full(len(F), 5.0)
    for _ in range(40):
        mid = (lo + hi) / 2
        p = bs_price(F, K, T, mid, call)
        hi = np.where(p > px_usd, mid, hi)
        lo = np.where(p > px_usd, lo, mid)
    iv = (lo + hi) / 2
    intrinsic = np.where(call, np.maximum(F - K, 0), np.maximum(K - F, 0))
    return np.where((px_usd > intrinsic * 1.0001) & (iv < 4.9) & (iv > 0.051), iv, np.nan)


MONTHS = {m: i for i, m in enumerate(["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"], 1)}


def parse_inst(name):
    _, exp, k, cp = name.split("-")
    day, mon, yr = int(exp[:-5]), MONTHS[exp[-5:-2]], 2000 + int(exp[-2:])
    return pd.Timestamp(yr, mon, day, 8, tz="UTC"), float(k), cp == "C"


def option_snapshot(trades, t_end):
    if len(trades) < 20:
        return None
    df = pd.DataFrame(trades, columns=["ts", "inst", "mark", "price", "iv", "index", "dir", "amount"])
    meta = [parse_inst(x) for x in df.inst]
    df["exp"] = [m[0] for m in meta]
    df["K"] = [m[1] for m in meta]
    df["call"] = [m[2] for m in meta]
    df["T"] = (df.exp - pd.Timestamp(t_end, unit="s", tz="UTC")).dt.total_seconds() / (365 * 86400)
    F = df["index"].to_numpy()
    df["iv_mark"] = implied_vol(df.mark.to_numpy() * F, F, df.K.to_numpy(), np.maximum(df["T"].to_numpy(), 1e-6), df.call.to_numpy())
    df["k"] = np.log(df.K / df["index"])
    out = {}
    s = df[(df["T"] > 3 / 365) & (df["T"] < 45 / 365) & df.iv_mark.notna() & (df.k.abs() < 0.3)]
    if len(s) >= 10 and s.k.min() < -0.03 and s.k.max() > 0.03:
        X = np.column_stack([np.ones(len(s)), s.k, s.k ** 2])
        coef = np.linalg.lstsq(X, s.iv_mark.to_numpy(), rcond=None)[0]
        out["atm_iv"] = coef[0]
        out["skew"] = (coef[0] - 0.1 * coef[1] + 0.01 * coef[2]) - (coef[0] + 0.1 * coef[1] + 0.01 * coef[2])  # 下方 10% − 上方 10%
    usd = df.amount * df["index"]
    buy = df.dir == "buy"
    cb, pb = usd[buy & df.call].sum(), usd[buy & ~df.call].sum()
    cs, ps = usd[~buy & df.call].sum(), usd[~buy & ~df.call].sum()
    tot = cb + pb + cs + ps
    if tot > 0:
        out["flow"] = ((cb - cs) - (pb - ps)) / tot  # 主动买看涨 − 主动买看跌，净额占比
        out["pcr"] = (pb + ps) / tot
    return out


def study_options(price, hf):
    files = sorted(glob.glob(os.path.join(HIST, "deribit_opt", "*.json")))
    rows = {}
    for f in files:
        t_end = int(os.path.basename(f)[:-5])
        snap = option_snapshot(json.load(open(f)), t_end)
        if snap:
            rows[pd.Timestamp(t_end, unit="s", tz="UTC")] = snap
    o = pd.DataFrame(rows).T.sort_index().astype(float)
    say(f"\n======== 3. 期权（Deribit BTC，每 4 小时一个点，{o.index[0]:%Y-%m-%d} – {o.index[-1]:%Y-%m-%d}，{len(o)} 个点，偏斜有值 {o['skew'].notna().sum()}）")
    dv = json.load(open(os.path.join(HIST, "dvol.json")))
    dvol = pd.Series([r[4] for r in dv], index=pd.to_datetime([r[0] for r in dv], unit="ms", utc=True) + pd.Timedelta(hours=1))
    rv = np.log(price.high / price.low).rolling(288).mean()  # 过去 24 小时平均 5 分钟振幅（只拿来比相对高低）
    hours = HORIZONS.copy()
    HORIZONS.clear()
    HORIZONS.update({"4h": 240, "24h": 1440})
    try:
        feats = {
            "25 delta 附近偏斜（下方 10% − 上方 10% 隐含波动率）": o["skew"],
            "偏斜 24 小时变化": o["skew"].diff(6),
            "平值隐含波动率": o["atm_iv"],
            "期权主动买看涨 − 买看跌（1 小时成交）": o["flow"],
            "看跌成交占比": o["pcr"],
        }
        for k, v in feats.items():
            direction_test(k, v, price.close, z_days=30)
        HORIZONS.clear()
        HORIZONS.update({"1h": 60, "4h": 240, "24h": 1440})
        direction_test("DVOL 24 小时变化", np.log(dvol).diff(24), price.close)
        vrp = np.log(dvol / 100) - np.log(rv.reindex(dvol.index, method="ffill") * math.sqrt(288 * 365))
        direction_test("DVOL 减已实现波动（波动风险溢价）", vrp, price.close)
    finally:
        HORIZONS.clear()
        HORIZONS.update(hours)
    hv = pd.DataFrame({"skew": at_hour(o["skew"]).ffill(limit=4), "atm_iv": np.log(at_hour(o["atm_iv"]).ffill(limit=4))})
    vol_test("期权偏斜和平值波动率", hv, hf)


def main(which):
    price = load_perp()
    say(f"价格：币安永续 5 分钟 {price.index[0]:%Y-%m-%d} – {price.index[-1]:%Y-%m-%d}；往返手续费按 {FEE_RT}%")
    hf = hourly_frame()
    if which in ("metrics", "all"):
        study_metrics(price, hf)
    if which in ("book", "all"):
        study_book(price, hf)
    if which in ("options", "all"):
        study_options(price, hf)
    overfit.report(TRIALS, say, FEE_RT)
    name = "results_alt_data.txt" if which == "all" else f"results_alt_data_{which}.txt"
    open(os.path.join(HERE, name), "w").write("\n".join(OUT) + "\n")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "all")
