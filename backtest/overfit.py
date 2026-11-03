"""过拟合和多重检验校正：试了很多组合以后，哪些「显著」还站得住。

按论文公式自己写（没有抄 GPL 代码）：
- Holm（控制「至少错一个」的概率）和 Benjamini–Hochberg–Yekutieli（控制错的比例，允许检验之间相关）
- Deflated Sharpe Ratio（Bailey & López de Prado 2014）：按试了多少次、收益偏度和厚尾，把夏普打折
- PBO，回测过拟合概率（Bailey, Borwein, López de Prado, Zhu 2017，CSCV）：
  把时间切成 S 块，每次拿一半挑「样本内最好」的配置，看它在另一半里排名是否掉到中位数以下
只依赖 numpy / pandas。
"""
import itertools
import math

import numpy as np
import pandas as pd


def norm_cdf(x):
    return 0.5 * math.erfc(-x / math.sqrt(2))


def norm_ppf(p):
    """标准正态分位数，二分法，精度够用。"""
    if p <= 0:
        return -math.inf
    if p >= 1:
        return math.inf
    lo, hi = -40.0, 40.0
    for _ in range(200):
        mid = (lo + hi) / 2
        if norm_cdf(mid) < p:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2


def ic_pvalue(ic, n):
    """秩相关的双侧 p 值（t 分布用正态近似，n 都很大）。"""
    if not np.isfinite(ic) or n < 4 or abs(ic) >= 1:
        return np.nan
    t = ic * math.sqrt((n - 2) / (1 - ic * ic))
    return 2 * (1 - norm_cdf(abs(t)))


def holm(p):
    p = np.asarray(p, dtype=float)
    out = np.full(len(p), np.nan)
    ok = np.isfinite(p)
    idx = np.where(ok)[0][np.argsort(p[ok])]
    m = len(idx)
    run = 0.0
    for k, i in enumerate(idx):
        run = max(run, min(1.0, (m - k) * p[i]))
        out[i] = run
    return out


def bhy(p):
    p = np.asarray(p, dtype=float)
    out = np.full(len(p), np.nan)
    ok = np.isfinite(p)
    idx = np.where(ok)[0][np.argsort(p[ok])]
    m = len(idx)
    if m == 0:
        return out
    c = sum(1.0 / k for k in range(1, m + 1))
    run = 1.0
    for k in range(m - 1, -1, -1):
        i = idx[k]
        run = min(run, min(1.0, p[i] * m * c / (k + 1)))
        out[i] = run
    return out


def sharpe_stats(x):
    x = np.asarray(x, dtype=float)
    x = x[np.isfinite(x)]
    if len(x) < 10 or x.std(ddof=1) == 0:
        return None
    sr = x.mean() / x.std(ddof=1)
    z = (x - x.mean()) / x.std(ddof=0)
    return {"sr": sr, "n": len(x), "skew": float((z ** 3).mean()), "kurt": float((z ** 4).mean())}


def expected_max_sr(var_sr, n_trials):
    """N 个夏普其实都是 0 时，纯靠运气能看到的最大值（期望）。"""
    if n_trials < 2 or var_sr <= 0:
        return 0.0
    g = 0.5772156649
    return math.sqrt(var_sr) * ((1 - g) * norm_ppf(1 - 1 / n_trials) + g * norm_ppf(1 - 1 / (n_trials * math.e)))


def deflated_sharpe(st, sr0):
    """夏普（按单笔/单期，不年化）比 sr0 真的更高的概率。"""
    sr, n, sk, ku = st["sr"], st["n"], st["skew"], st["kurt"]
    den = 1 - sk * sr + (ku - 1) / 4 * sr * sr
    if den <= 0:
        return np.nan
    return norm_cdf((sr - sr0) * math.sqrt(n - 1) / math.sqrt(den))


def pbo(mat, s=16):
    """mat：时间 × 配置 的收益（DataFrame）。返回 PBO 和每次组合样本外的相对名次。"""
    m = mat.dropna(axis=1, how="all").fillna(0.0)
    if m.shape[1] < 2 or len(m) < s * 5:
        return np.nan, {}
    blocks = np.array_split(np.arange(len(m)), s)
    vals = m.to_numpy()
    lam, oos_sr, oos_mean = [], [], []
    for combo in itertools.combinations(range(s), s // 2):
        ins = np.concatenate([blocks[i] for i in combo])
        oos = np.concatenate([blocks[i] for i in range(s) if i not in combo])
        def sr(rows):
            x = vals[rows]
            sd = x.std(axis=0, ddof=1)
            return np.where(sd > 0, x.mean(axis=0) / np.where(sd > 0, sd, 1), -np.inf)
        best = int(np.argmax(sr(ins)))
        o = sr(oos)
        rank = (o < o[best]).sum() + 0.5 * ((o == o[best]).sum() - 1)
        w = (rank + 1) / (len(o) + 1)
        lam.append(math.log(w / (1 - w)))
        oos_sr.append(o[best])
        oos_mean.append(vals[oos, best].mean())
    lam = np.array(lam)
    return float((lam <= 0).mean()), {"lam": lam, "oos_sr": np.array(oos_sr), "oos_mean": np.array(oos_mean)}


def report(trials, say, fee=0.10):
    """trials：每个 {name, hz, ic, n, pnl}，pnl 是按时间标的单笔扣费收益（%）。"""
    say("\n======== 过拟合和多重检验校正")
    say(f"一共试了 {len(trials)} 个「因子 × 周期」组合。下面把它们放在一起校正，不再一个一个看。")
    p = np.array([ic_pvalue(t["ic"], t["n"]) for t in trials])
    ph, pb = holm(p), bhy(p)
    say("\n### 1. 预测力（IC）显著性：原始 p 值 → Holm / BHY 校正后")
    order = np.argsort(np.where(np.isfinite(p), p, 9))
    keep = 0
    for i in order:
        t = trials[i]
        if not np.isfinite(p[i]):
            continue
        mark = "✓ 校正后仍显著" if pb[i] < 0.05 else ("（只有原始 p 显著）" if p[i] < 0.05 else "")
        if p[i] < 0.05 or keep < 5:
            say(f"  {t['name']} · {t['hz']}: IC {t['ic']:+.3f}，原始 p {p[i]:.1e}，Holm {ph[i]:.1e}，BHY {pb[i]:.1e} {mark}")
            keep += 1
    n_raw, n_bhy, n_holm = int((p < 0.05).sum()), int((pb < 0.05).sum()), int((ph < 0.05).sum())
    say(f"  原始 p<0.05：{n_raw} 个；BHY 校正后：{n_bhy} 个；Holm 校正后：{n_holm} 个。")

    fee_txt = f"往返 {fee}%" if isinstance(fee, (int, float)) else fee
    say(f"\n### 2. 打法收益：Deflated Sharpe（按试的次数把夏普打折，2024 年以后、手续费{fee_txt}）")
    stats = []
    for t in trials:
        st = sharpe_stats(t["pnl"].to_numpy()) if len(t["pnl"]) else None
        if st and st["n"] >= 30:
            stats.append((t, st))
    if not stats:
        say("  没有足够笔数的打法。")
        return
    srs = np.array([s["sr"] for _, s in stats])
    sr0 = expected_max_sr(float(srs.var(ddof=1)), len(stats))
    say(f"  {len(stats)} 个打法里，单笔夏普纯靠运气能见到的最大值约 {sr0:.3f}。")
    rows = sorted(stats, key=lambda x: -x[1]["sr"])
    for t, st in rows[:8]:
        d = deflated_sharpe(st, sr0)
        mean = float(np.mean(t["pnl"]))
        verdict = "可能是真的" if d > 0.95 else "运气解释得通"
        say(f"  {t['name']} · {t['hz']}: {st['n']} 笔，扣费 {mean:+.3f}%/笔，单笔夏普 {st['sr']:+.3f}，"
            f"DSR {d:.2f}（{verdict}）")
    winners = [(t, st) for t, st in stats if np.mean(t["pnl"]) > 0]
    say(f"  扣费后平均为正的打法：{len(winners)} / {len(stats)} 个；DSR > 0.95 的：{sum(deflated_sharpe(st, sr0) > 0.95 for _, st in stats)} 个。")

    say("\n### 3. PBO 回测过拟合概率（2024 年以后，按天汇总每个打法的扣费收益，CSCV 切 16 块）")
    cols = {}
    for t, _ in stats:
        s = t["pnl"].groupby(t["pnl"].index.floor("D")).sum()
        cols[f"{t['name']}·{t['hz']}"] = s
    mat = pd.DataFrame(cols).sort_index()
    mat = mat[mat.index >= pd.Timestamp("2024-01-01", tz="UTC")]
    mat = mat.reindex(pd.date_range(mat.index[0], mat.index[-1], freq="D", tz="UTC"))
    val, info = pbo(mat)
    if np.isfinite(val):
        om = info["oos_mean"]
        say(f"  {mat.shape[1]} 个打法、{len(mat)} 天、{len(om)} 种切法：PBO = {val:.2f}。"
            "意思是「挑出样本内最好的那个，到样本外跌到中位数以下」的概率。")
        say(f"  但 PBO 只看名次：样本内最好的那个，到样本外平均每天 {om.mean():+.3f}%，"
            f"样本外为正的切法只占 {(om > 0).mean() * 100:.0f}%。")
        say("  PBO 低在这里的意思是「频繁交易的打法总被手续费压在最后」，名次很稳定；不等于挑出来的那个能赚钱。")
    else:
        say("  数据不够算 PBO。")
