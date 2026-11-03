"""未来 1 小时 / 4 小时振幅模型。

和第一轮实验一样：用币安现货 1 小时 K 线的已实现振幅（HAR）、成交量、
钟点和 Deribit DVOL，岭回归预测下一段的 ln(最高/最低)。特征先按训练窗
标准化。权重每次拟合算出来，不写死。

走步：先攒 30 天，之后每次往前 7 天；训练只用预测窗口开始之前的数据，
避免偷看未来。样本外 R² 在对数振幅上计算，和实验同一口径。

只用 numpy / pandas，岭回归用闭式解，对应 sklearn 的
Ridge(alpha=1.0, fit_intercept=True)。
"""
import math

import numpy as np
import pandas as pd

ALPHA = 1.0
TRAIN_DAYS = 30
STEP_DAYS = 7
# 和面板「我的持仓」里强平距离同一口径：维持保证金 0.4%
MAINT_MARGIN = 0.004
HALF_LIQ = 0.5
LEVERAGES = (20, 40, 50, 100)
Q_LABELS = {1: "很安静", 2: "偏静", 3: "中等", 4: "偏大", 5: "很大"}

# 预测用的全部因子。前 4 个是 HAR（只看过去振幅）
VOL_FEATURES = [
    "rv1", "rv4", "rv24", "rv168", "logvol_rel",
    "us_session_next", "weekend", "hour_sin", "hour_cos", "dvol",
]
HAR_FEATURES = VOL_FEATURES[:4]
FULL_FEATURES = VOL_FEATURES[:-1]  # 不含 DVOL

FACTOR_ZH = {
    "rv1": "过去 1 小时振幅",
    "rv4": "过去 4 小时平均振幅",
    "rv24": "过去 24 小时平均振幅",
    "rv168": "过去 7 天平均振幅",
    "logvol_rel": "成交量相对近 7 天",
    "us_session_next": "下一小时在美股交易时段",
    "weekend": "周末",
    "hour_sin": "钟点（正弦）",
    "hour_cos": "钟点（余弦）",
    "dvol": "Deribit 波动率指数",
}


def ridge_fit(X, y, alpha=ALPHA):
    """标准化之后的 X（n, p）拟合 y。返回权重、截距。"""
    X = np.asarray(X, dtype=float)
    y = np.asarray(y, dtype=float)
    x_mean = X.mean(axis=0)
    y_mean = float(y.mean())
    xc = X - x_mean
    yc = y - y_mean
    p = X.shape[1]
    xtx = xc.T @ xc
    xtx.flat[:: p + 1] += alpha
    try:
        w = np.linalg.solve(xtx, xc.T @ yc)
    except np.linalg.LinAlgError:
        w = np.linalg.lstsq(xtx, xc.T @ yc, rcond=None)[0]
    intercept = y_mean - float(x_mean @ w)
    return w, float(intercept)


def ridge_predict(X, w, intercept):
    return np.asarray(X, dtype=float) @ np.asarray(w, dtype=float) + intercept


def r2_score(y, pred):
    y = np.asarray(y, dtype=float)
    pred = np.asarray(pred, dtype=float)
    ss_tot = float(((y - y.mean()) ** 2).sum())
    if ss_tot <= 0:
        return None
    ss_res = float(((y - pred) ** 2).sum())
    return 1.0 - ss_res / ss_tot


def log_range_pct(log_pred):
    """模型输出是 ln(ln(高/低))。换成实验里的振幅%：ln(高/低)×100。"""
    return float(np.exp(log_pred) * 100.0)


def future_log_range(high, low, horizon):
    """时刻 t 之后 horizon 根已完成小时的 ln(最高/最低)，不含当根。"""
    fut_hi = high[::-1].rolling(horizon).max()[::-1].shift(-1)
    fut_lo = low[::-1].rolling(horizon).min()[::-1].shift(-1)
    return np.log(fut_hi / fut_lo)


def _as_utc_index(index):
    idx = pd.DatetimeIndex(index)
    if idx.tz is None:
        idx = idx.tz_localize("UTC")
    else:
        idx = idx.tz_convert("UTC")
    return idx


def features_from_hourly(hourly):
    """hourly 需有 open/high/low/close/volume，可选 dvol（指数点位，不是对数）。

    只使用当根及更早的数据。最后一根如果是未走完的小时，调用方应先丢掉。
    """
    h = hourly.copy()
    h.index = _as_utc_index(h.index)
    h = h.sort_index()
    h = h[~h.index.duplicated(keep="last")]
    lr = np.log(h["high"] / h["low"])
    x = pd.DataFrame(index=h.index)
    x["rv1"] = np.log(lr.replace(0, np.nan))
    x["rv4"] = np.log(lr.rolling(4).mean())
    x["rv24"] = np.log(lr.rolling(24).mean())
    x["rv168"] = np.log(lr.rolling(168).mean())
    med = h["volume"].rolling(168).median()
    x["logvol_rel"] = np.log(h["volume"] / med.replace(0, np.nan))
    ny = h.index.tz_convert("America/New_York")
    nxt = ny + pd.Timedelta(hours=1)
    x["us_session_next"] = ((nxt.weekday < 5) & (nxt.hour >= 9) & (nxt.hour < 16)).astype(float)
    x["weekend"] = (ny.weekday >= 5).astype(float)
    hour = h.index.hour.astype(float)
    x["hour_sin"] = np.sin(2 * np.pi * hour / 24.0)
    x["hour_cos"] = np.cos(2 * np.pi * hour / 24.0)
    if "dvol" in h.columns:
        x["dvol"] = np.log(h["dvol"].where(h["dvol"] > 0))
    else:
        x["dvol"] = np.nan
    x["y1"] = future_log_range(h["high"], h["low"], 1)
    x["y4"] = future_log_range(h["high"], h["low"], 4)
    x["close"] = h["close"]
    x["dvol_level"] = h["dvol"] if "dvol" in h.columns else np.nan
    x["hour_range_pct"] = lr * 100.0
    return x.replace([np.inf, -np.inf], np.nan)


def hourly_from_klines(rows):
    """币安 K 线（5 分钟或 1 小时均可）合成 1 小时。rows 为 DataFrame 或 list of lists。"""
    if isinstance(rows, pd.DataFrame):
        df = rows.copy()
        if "t" not in df.columns:
            raise ValueError("K 线缺少 t 列")
    else:
        df = pd.DataFrame(list(rows))
        if df.empty:
            return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])
        df = df.iloc[:, :11]
        df.columns = ["t", "o", "h", "l", "c", "v", "ct", "qv", "n", "tbv", "tbqv"][: df.shape[1]]
    for col in ("o", "h", "l", "c", "v"):
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")
    df["t"] = pd.to_numeric(df["t"], errors="coerce")
    df = df.dropna(subset=["t", "o", "h", "l", "c"])
    df = df.drop_duplicates("t")
    df.index = pd.to_datetime(df["t"], unit="ms", utc=True)
    hourly = df.resample("1h").agg(open=("o", "first"), high=("h", "max"), low=("l", "min"),
                                   close=("c", "last"), volume=("v", "sum"))
    hourly = hourly.dropna(subset=["open", "high", "low", "close"])
    now = pd.Timestamp.now(tz="UTC")
    hourly = hourly[hourly.index + pd.Timedelta(hours=1) <= now]
    return hourly


def attach_dvol(hourly, dvol_rows):
    """dvol_rows: DataFrame 含 t、c，或 [[ts_ms, o, h, l, c], ...]。"""
    out = hourly.copy()
    if isinstance(dvol_rows, pd.DataFrame):
        dv = dvol_rows.copy()
    elif not dvol_rows:
        out["dvol"] = np.nan
        return out
    else:
        width = len(dvol_rows[0])
        dv = pd.DataFrame(list(dvol_rows), columns=["t", "o", "h", "l", "c"][:width])
    if dv.empty or "t" not in dv.columns:
        out["dvol"] = np.nan
        return out
    dv["t"] = pd.to_numeric(dv["t"], errors="coerce")
    dv["c"] = pd.to_numeric(dv["c"] if "c" in dv.columns else dv.iloc[:, -1], errors="coerce")
    dv = dv.dropna(subset=["t", "c"]).drop_duplicates("t")
    dv.index = pd.to_datetime(dv["t"], unit="ms", utc=True)
    out["dvol"] = dv["c"].reindex(out.index).ffill()
    return out


def _standardize_fit(train, cols):
    mu = train[cols].mean()
    sd = train[cols].std(ddof=1).replace(0, 1.0)
    sd = sd.fillna(1.0)
    return mu, sd


def _apply_std(df, cols, mu, sd):
    return (df[cols] - mu) / sd


def _fit_cols(train, cols, y):
    mu, sd = _standardize_fit(train, cols)
    xz = _apply_std(train, cols, mu, sd)
    w, intercept = ridge_fit(xz.to_numpy(), y.to_numpy(), ALPHA)
    return {"features": list(cols), "mu": {c: float(mu[c]) for c in cols},
            "sd": {c: float(sd[c]) for c in cols}, "weights": {c: float(wi) for c, wi in zip(cols, w)},
            "intercept": intercept, "alpha": ALPHA}


def _predict_pack(pack, df):
    cols = pack["features"]
    mu = pd.Series(pack["mu"])
    sd = pd.Series(pack["sd"])
    xz = _apply_std(df, cols, mu, sd)
    w = np.array([pack["weights"][c] for c in cols])
    return ridge_predict(xz.to_numpy(), w, pack["intercept"])


def walk_forward(frame, horizon_hours=4, target="y4", with_dvol=True):
    """返回样本外预测表和指标。预测列：naive、har、full、full_dvol（有 DVOL 时）。"""
    ycol = target
    cols = VOL_FEATURES if with_dvol else FULL_FEATURES
    need = cols + [ycol]
    d = frame.dropna(subset=need).copy()
    d = d[d[ycol] > 0]
    if d.empty:
        return d, _empty_metrics()
    d["log_y"] = np.log(d[ycol])
    idx = d.index
    start = idx[0] + pd.Timedelta(days=TRAIN_DAYS)
    parts = []
    t = start
    end = idx[-1]
    sets = [("har", HAR_FEATURES), ("full", FULL_FEATURES)]
    if with_dvol:
        sets.append(("full_dvol", VOL_FEATURES))
    while t <= end:
        tr = d[d.index < t - pd.Timedelta(hours=horizon_hours)]
        te = d[(d.index >= t) & (d.index < t + pd.Timedelta(days=STEP_DAYS))]
        if len(te) == 0:
            break
        if len(tr) < 24 * 7:
            t += pd.Timedelta(days=STEP_DAYS)
            continue
        block = te[[ycol, "log_y"]].copy()
        block["naive"] = float(tr["log_y"].mean())
        ytr = tr["log_y"]
        for name, feats in sets:
            pack = _fit_cols(tr, feats, ytr)
            block[name] = _predict_pack(pack, te)
        parts.append(block)
        t += pd.Timedelta(days=STEP_DAYS)
    if not parts:
        return d.iloc[0:0], _empty_metrics()
    oos = pd.concat(parts)
    oos = oos[~oos.index.duplicated(keep="first")]
    return oos, metrics_from_oos(oos, with_dvol=with_dvol, y_col=ycol)


def oos_r2(frame, features, target="y4", horizon_hours=4):
    """单一因子组合的走步样本外 R²。行必须已经含这些列和目标。"""
    feats = list(features)
    d = frame.dropna(subset=feats + [target]).copy()
    d = d[d[target] > 0]
    if len(d) < 24 * 14:
        return None, 0
    d["log_y"] = np.log(d[target])
    idx = d.index
    start = idx[0] + pd.Timedelta(days=TRAIN_DAYS)
    ys, ps = [], []
    t = start
    while t <= idx[-1]:
        tr = d[d.index < t - pd.Timedelta(hours=horizon_hours)]
        te = d[(d.index >= t) & (d.index < t + pd.Timedelta(days=STEP_DAYS))]
        if len(te) == 0:
            break
        if len(tr) < 24 * 7:
            t += pd.Timedelta(days=STEP_DAYS)
            continue
        pack = _fit_cols(tr, feats, tr["log_y"])
        pred = _predict_pack(pack, te)
        ys.append(te["log_y"].to_numpy())
        ps.append(np.asarray(pred, dtype=float))
        t += pd.Timedelta(days=STEP_DAYS)
    if not ys:
        return None, 0
    y = np.concatenate(ys)
    p = np.concatenate(ps)
    val = r2_score(y, p)
    return (None if val is None else float(val)), int(len(y))


def _empty_metrics():
    return {"oos_start": None, "oos_end": None, "n": 0, "folds_ok": False,
            "r2": {"naive": None, "har": None, "full": None, "full_dvol": None},
            "quintiles": [], "q_edges_log": [], "pred_col": None}


def _quintile_edges(pred):
    pred = np.asarray(pred, dtype=float)
    pred = pred[np.isfinite(pred)]
    if len(pred) < 20:
        return []
    edges = np.quantile(pred, [0.2, 0.4, 0.6, 0.8])
    for i in range(1, len(edges)):
        if edges[i] <= edges[i - 1]:
            edges[i] = edges[i - 1] + 1e-9
    return [float(x) for x in edges]


def assign_quintile(pred, edges):
    """1 = 历史预测里最低的一档，5 = 最高。edges 是 20/40/60/80 分位。"""
    if pred is None or not np.isfinite(pred) or not edges:
        return None
    return int(np.searchsorted(np.asarray(edges, dtype=float), float(pred), side="right") + 1)


def metrics_from_oos(oos, with_dvol=True, y_col=None):
    """oos 里第一组目标列是 ln(高/低)，log_y 是它的对数，其余是预测。"""
    pred_col = "full_dvol" if with_dvol and "full_dvol" in oos.columns else "full"
    if y_col is None:
        y_col = "y4" if "y4" in oos.columns else ("y1" if "y1" in oos.columns else oos.columns[0])
    y = oos["log_y"]
    r2 = {}
    for name in ("naive", "har", "full", "full_dvol"):
        if name not in oos.columns:
            r2[name] = None
            continue
        val = r2_score(y, oos[name])
        r2[name] = None if val is None else round(float(val), 3)
    edges = _quintile_edges(oos[pred_col]) if pred_col in oos.columns else []
    quintiles = []
    if edges and pred_col in oos.columns:
        q = oos[pred_col].map(lambda v: assign_quintile(v, edges))
        y_range = oos[y_col]
        for qi, g in oos.groupby(q):
            if qi is None or isinstance(qi, float) and not np.isfinite(qi):
                continue
            quintiles.append({
                "q": int(qi),
                "label": Q_LABELS.get(int(qi), ""),
                "n": int(len(g)),
                "pred_med_pct": round(float(np.median(np.exp(g[pred_col].to_numpy()) * 100.0)), 3),
                "actual_med_pct": round(float(np.median(y_range.loc[g.index].to_numpy()) * 100.0), 3),
            })
    return {
        "oos_start": None if oos.empty else str(oos.index[0]),
        "oos_end": None if oos.empty else str(oos.index[-1]),
        "n": int(len(oos)),
        "folds_ok": len(oos) > 0,
        "r2": r2,
        "quintiles": quintiles,
        "q_edges_log": edges,
        "pred_col": pred_col,
        "median_actual_pct": None if oos.empty else round(float(oos[y_col].median() * 100.0), 3),
    }


def fit_production(frame, horizon_hours, target, with_dvol):
    """用全部已经能看到结果的小时拟合。最后一根没有未来振幅，不进训练。"""
    cols = VOL_FEATURES if with_dvol else FULL_FEATURES
    d = frame.dropna(subset=cols + [target]).copy()
    d = d[d[target] > 0]
    if len(d) < 24 * 7:
        raise RuntimeError(f"可训练的小时只有 {len(d)}，不够拟合")
    d["log_y"] = np.log(d[target])
    pack = _fit_cols(d, cols, d["log_y"])
    pack["n_train"] = int(len(d))
    pack["train_start"] = str(d.index[0])
    pack["train_end"] = str(d.index[-1])
    pack["horizon_hours"] = int(horizon_hours)
    pack["target"] = target
    return pack


def predict_latest(pack, frame):
    """用最近一根因子齐全的小时做预测。"""
    cols = pack["features"]
    ready = frame.dropna(subset=cols)
    if ready.empty:
        raise RuntimeError("最近没有因子齐全的小时，不能预测")
    row = ready.iloc[[-1]]
    log_pred = float(_predict_pack(pack, row)[0])
    return row.index[-1], log_pred, row.iloc[0]


def describe_factor(key, row):
    """此刻这个因子用白话怎么读。"""
    if key == "dvol":
        lvl = row.get("dvol_level")
        if lvl is not None and np.isfinite(lvl):
            return f"此刻 {float(lvl):.1f}"
        if np.isfinite(row.get("dvol", np.nan)):
            return f"此刻 {math.exp(float(row['dvol'])):.1f}"
        return "没有"
    if key == "rv1" and np.isfinite(row.get("rv1", np.nan)):
        return f"上一小时 {math.exp(float(row['rv1'])) * 100:.2f}%"
    if key in ("rv4", "rv24", "rv168") and np.isfinite(row.get(key, np.nan)):
        return f"对数均值 {float(row[key]):.2f}"
    if key == "logvol_rel" and np.isfinite(row.get(key, np.nan)):
        return f"成交量是近 7 天中位的 {math.exp(float(row[key])):.2f} 倍"
    if key == "us_session_next":
        return "是" if row.get("us_session_next", 0) >= 0.5 else "不是"
    if key == "weekend":
        return "是" if row.get("weekend", 0) >= 0.5 else "不是"
    if key in ("hour_sin", "hour_cos") and np.isfinite(row.get(key, np.nan)):
        return f"{float(row[key]):+.2f}"
    return "—"


def equation_rows(pack, row, log_pred):
    cols = pack["features"]
    mu = pack["mu"]
    sd = pack["sd"]
    rows = []
    contrib_sum = 0.0
    for key in cols:
        raw = float(row[key])
        z = (raw - mu[key]) / sd[key] if sd[key] else 0.0
        w = float(pack["weights"][key])
        contrib = w * z
        contrib_sum += contrib
        rows.append({
            "key": key,
            "name": FACTOR_ZH.get(key, key),
            "weight": round(w, 6),
            "now": None if not np.isfinite(raw) else round(raw, 6),
            "now_text": describe_factor(key, row),
            "z": round(z, 4),
            "contrib": round(contrib, 6),
            "amp_mult_per_std": round(math.exp(w), 4),
        })
    return rows, round(contrib_sum + pack["intercept"], 6)


def liq_distance_pct(leverage, maint=MAINT_MARGIN):
    """强平距离（%）。和面板 pnlU 里 1/杠杆 − 0.4% 相同。"""
    return (1.0 / float(leverage) - maint) * 100.0


def max_leverage_half_buffer(stop_pct, half=HALF_LIQ, maint=MAINT_MARGIN):
    """止损距离小于强平距离的 half 时，杠杆最高能到多少（向下取整）。"""
    if stop_pct is None or not np.isfinite(stop_pct) or stop_pct <= 0:
        return None
    need = (stop_pct / 100.0) / half + maint
    if need <= 0:
        return None
    lev = int(math.floor(1.0 / need))
    return lev if lev >= 1 else 0


def practical_block(stop_pct, price):
    stop_pct = float(stop_pct)
    usd = None if price is None or not np.isfinite(price) else round(float(price) * stop_pct / 100.0, 2)
    rows = []
    for lev in LEVERAGES:
        dist = liq_distance_pct(lev)
        half = dist * HALF_LIQ
        rows.append({
            "lev": lev,
            "liq_distance_pct": round(dist, 4),
            "half_liq_pct": round(half, 4),
            "stop_below_half_liq": bool(dist > 0 and stop_pct < half),
            "normal_move_reaches_liq": bool(dist <= 0 or stop_pct >= dist),
        })
    return {
        "stop_pct": round(stop_pct, 4),
        "stop_usd": usd,
        "price": None if price is None else round(float(price), 2),
        "basis": "参考止损距离 = 未来 4 小时预计振幅。这是正常波动大概会走出的高低跨度，用来对照强平距离，不是下单指令。",
        "maintenance_margin_pct": MAINT_MARGIN * 100.0,
        "half_fraction": HALF_LIQ,
        "max_leverage_stop_below_half_liq": max_leverage_half_buffer(stop_pct),
        "leverage": rows,
    }


def production_feature_set(with_dvol):
    return "full_dvol" if with_dvol else "full"


def round_weights(pack, nd=3):
    return {k: round(float(v), nd) for k, v in pack["weights"].items()}


def summarize_zh(h4, h1):
    def one(block, label):
        r = (block or {}).get("oos", {}).get("r2") or {}
        parts = [f"{label}样本外 R² { _fmt(r.get('full_dvol') if r.get('full_dvol') is not None else r.get('full')) }"]
        parts.append(f"只看过去振幅 {_fmt(r.get('har'))}")
        parts.append(f"加时段和成交量 {_fmt(r.get('full'))}")
        if r.get("full_dvol") is not None:
            parts.append(f"再加 DVOL {_fmt(r.get('full_dvol'))}")
        n = (block or {}).get("oos", {}).get("n")
        return "，".join(parts) + (f"（{n} 个小时）" if n else "")

    return one(h4, "4 小时") + "。" + one(h1, "1 小时") + "。权重是这次拟合得到的，不是手填的。"


def _fmt(v):
    return "—" if v is None else f"{v:.3f}"
