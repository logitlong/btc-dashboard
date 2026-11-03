"""等因子快照攒够之后，把它们接进波动尺子同一套走步，看每个因子多出来的样本外 R²。

快照在 factor-history 分支的 snapshots/年-月-日.jsonl。建议至少 2 到 3 个月再下结论：
走步要先留 30 天做训练，再一周一周往前推。数据不够时只报告有多少小时，不装成已经测完。

用法：
  python factor_oos.py --self-test
  python factor_oos.py --snapshots /path/to/snapshots --klines /path/to/klines_5m.csv
  python factor_oos.py --snapshots /path/to/snapshots          # 不给 K 线就按快照的时间去拉币安公开行情

K 线 csv 可以是币安原始列（t o h l c v ...），也可以带表头 t,o,h,l,c,v。
"""
import argparse
import json
import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "monitor"))
import vol_model as M

# 快照里抽出来、准备叠到振幅模型上的因子。(列名, 中文)
CANDIDATES = [
    ("dvol_snap", "Deribit DVOL（快照）"),
    ("atm_iv", "平值隐含波动率"),
    ("skew_25d", "25 delta 偏斜"),
    ("max_pain_dist", "最大痛点相对现货的对数距离"),
    ("imb_05", "盘口 ±0.5% 失衡"),
    ("imb_10", "盘口 ±1% 失衡"),
    ("cvd_imb", "15 分钟主动买卖差占成交量"),
    ("funding_okx", "OKX 资金费率"),
    ("funding_bitget", "Bitget 资金费率"),
    ("funding_gate", "Gate 资金费率"),
    ("funding_hl", "Hyperliquid 资金费率（1 小时）"),
    ("basis_okx", "OKX 永续-现货基差"),
    ("basis_bitget", "Bitget 永续-现货基差"),
    ("basis_gate", "Gate 永续-现货基差"),
    ("basis_hl", "Hyperliquid 标记价-预言机基差"),
    ("coinbase_premium", "Coinbase 溢价"),
]
MIN_ALIGNED_HOURS = 24 * 14
MIN_OOS = 24 * 7


def _num(v):
    try:
        x = float(v)
    except (TypeError, ValueError):
        return np.nan
    return x if np.isfinite(x) else np.nan


def load_snapshots(path):
    files = []
    if os.path.isdir(path):
        for name in sorted(os.listdir(path)):
            if name.endswith(".jsonl"):
                files.append(os.path.join(path, name))
    elif path.endswith(".jsonl"):
        files.append(path)
    else:
        raise SystemExit(f"找不到快照：{path}")
    rows = []
    for fp in files:
        with open(fp, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rows.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    return rows


def _align_hour(ts):
    """快照时间落到「已经走完、可以用来预测下一截」的那根小时。

    10:15、10:45、11:00 都算 10:00 这根（11:00 才知道这根收盘）。
    """
    ts = pd.Timestamp(ts)
    if ts.tzinfo is None:
        ts = ts.tz_localize("UTC")
    else:
        ts = ts.tz_convert("UTC")
    if ts.minute == 0 and ts.second == 0 and ts.microsecond == 0 and ts.nanosecond == 0:
        return ts - pd.Timedelta(hours=1)
    return ts.floor("1h")


def snapshot_factors(snaps):
    """每根小时保留该小时内最后一条快照，抽出数值因子。"""
    recs = []
    for s in snaps:
        if not s.get("t"):
            continue
        deribit = s.get("deribit") if isinstance(s.get("deribit"), dict) else {}
        book = s.get("book") if isinstance(s.get("book"), dict) else {}
        cvd = s.get("cvd_15m") if isinstance(s.get("cvd_15m"), dict) else {}
        ex = s.get("exchanges") if isinstance(s.get("exchanges"), dict) else {}
        spot = _num(s.get("spot"))
        pain = _num(deribit.get("max_pain")) if "error" not in deribit else np.nan
        vol = _num(cvd.get("volume")) if "error" not in cvd else np.nan
        cvd_v = _num(cvd.get("cvd")) if "error" not in cvd else np.nan

        def ex_field(name, field):
            block = ex.get(name)
            if not isinstance(block, dict) or "error" in block:
                return np.nan
            return _num(block.get(field))

        recs.append({
            "hour": _align_hour(s["t"]),
            "dvol_snap": _num(deribit.get("dvol")) if "error" not in deribit else np.nan,
            "atm_iv": _num(deribit.get("atm_iv")) if "error" not in deribit else np.nan,
            "skew_25d": _num(deribit.get("skew_25d")) if "error" not in deribit else np.nan,
            "max_pain_dist": np.log(pain / spot) if pain > 0 and spot > 0 else np.nan,
            "imb_05": _num(book.get("imb_05")) if "error" not in book else np.nan,
            "imb_10": _num(book.get("imb_10")) if "error" not in book else np.nan,
            "cvd_imb": (cvd_v / vol) if vol and np.isfinite(vol) and vol > 0 and np.isfinite(cvd_v) else np.nan,
            "funding_okx": ex_field("okx", "funding"),
            "funding_bitget": ex_field("bitget", "funding"),
            "funding_gate": ex_field("gate", "funding"),
            "funding_hl": ex_field("hyperliquid", "funding"),
            "basis_okx": ex_field("okx", "basis_pct"),
            "basis_bitget": ex_field("bitget", "basis_pct"),
            "basis_gate": ex_field("gate", "basis_pct"),
            "basis_hl": ex_field("hyperliquid", "basis_pct"),
            "coinbase_premium": _num(s.get("coinbase_premium_pct")),
        })
    if not recs:
        return pd.DataFrame()
    df = pd.DataFrame(recs).sort_values("hour")
    df = df.groupby("hour", as_index=True).last()
    df.index = pd.DatetimeIndex(df.index)
    if df.index.tz is None:
        df.index = df.index.tz_localize("UTC")
    return df.sort_index()


def load_klines(path):
    df = pd.read_csv(path)
    if "t" not in df.columns:
        df = pd.read_csv(path, header=None)
        cols = ["t", "o", "h", "l", "c", "v", "ct", "qv", "n", "tbv", "tbqv"]
        df.columns = cols[: df.shape[1]]
    return df


def build_joined(snaps, kline_rows, dvol_rows=None):
    factors = snapshot_factors(snaps)
    hourly = M.hourly_from_klines(kline_rows)
    if dvol_rows is not None:
        hourly = M.attach_dvol(hourly, dvol_rows)
    elif "dvol_snap" in factors.columns:
        hourly = hourly.copy()
        hourly["dvol"] = factors["dvol_snap"].reindex(hourly.index)
    frame = M.features_from_hourly(hourly)
    if factors.empty:
        return frame
    extra = [c for c, _ in CANDIDATES if c in factors.columns]
    aligned = factors[extra].reindex(frame.index).ffill(limit=2)
    for col in extra:
        frame[col] = aligned[col]
    return frame


def incremental_report(frame, target="y4", horizon_hours=4):
    """每个候选因子相对「波动尺子同一套因子」的样本外 R² 增量。"""
    has_dvol = bool(frame["dvol"].notna().sum() > 24 * 7) if "dvol" in frame.columns else False
    base = M.VOL_FEATURES if has_dvol else M.FULL_FEATURES
    base = [c for c in base if c in frame.columns]
    rows = []
    usable = frame.dropna(subset=base + [target])
    base_r2, base_n = M.oos_r2(usable, base, target, horizon_hours)
    rows.append({
        "factor": "（基准）",
        "key": "baseline",
        "r2": None if base_r2 is None else round(base_r2, 4),
        "delta_r2": 0.0 if base_r2 is not None else None,
        "n_oos": base_n,
        "n_aligned": int(len(usable)),
        "note": "波动尺子的因子，不含下面这些快照列" if base_r2 is not None else "基准样本还不够做走步",
    })
    for key, name in CANDIDATES:
        if key not in frame.columns:
            rows.append({"factor": name, "key": key, "r2": None, "delta_r2": None, "n_oos": 0,
                         "n_aligned": 0, "note": "快照里还没有这一列"})
            continue
        sub = frame.dropna(subset=base + [target, key])
        n_aligned = int(len(sub))
        if n_aligned < MIN_ALIGNED_HOURS:
            rows.append({"factor": name, "key": key, "r2": None, "delta_r2": None, "n_oos": 0,
                         "n_aligned": n_aligned,
                         "note": f"对齐后只有 {n_aligned} 小时，少于 {MIN_ALIGNED_HOURS}，先继续记"})
            continue
        r_base, n_base = M.oos_r2(sub, base, target, horizon_hours)
        r_new, n_new = M.oos_r2(sub, base + [key], target, horizon_hours)
        if r_base is None or r_new is None or n_new < MIN_OOS:
            rows.append({"factor": name, "key": key, "r2": None, "delta_r2": None, "n_oos": n_new,
                         "n_aligned": n_aligned, "note": "走步窗口还太短（训练要 30 天，样本外至少再留一周）"})
            continue
        rows.append({
            "factor": name, "key": key,
            "r2": round(r_new, 4),
            "delta_r2": round(r_new - r_base, 4),
            "n_oos": n_new,
            "n_aligned": n_aligned,
            "note": "同一批小时上，基准 R² " + f"{r_base:.4f}",
        })
    return {
        "target": "未来 4 小时振幅" if target == "y4" else target,
        "horizon_hours": horizon_hours,
        "baseline_features": base,
        "baseline_r2": None if base_r2 is None else round(base_r2, 4),
        "factors": rows,
    }


def format_report(report):
    lines = [
        f"目标：{report['target']}。基准样本外 R²：{report['baseline_r2'] if report['baseline_r2'] is not None else '还不够'}。",
        "增量是「加上这个因子之后的 R²」减去「同一批小时、不加它的 R²」。正数才是多出来的解释力。",
        "",
        f"{'因子':<28}{'R²':>10}{'增量':>10}{'样本外小时':>12}  说明",
    ]
    for row in report["factors"]:
        r2 = "—" if row["r2"] is None else f"{row['r2']:.4f}"
        dlt = "—" if row["delta_r2"] is None else f"{row['delta_r2']:+.4f}"
        lines.append(f"{row['factor']:<28}{r2:>10}{dlt:>10}{row['n_oos']:>12}  {row['note']}")
    if report["baseline_r2"] is None:
        lines.append("")
        lines.append("快照还不够。每天大约 24 个小时，走步要先攒 30 天，建议满 2 到 3 个月再看增量。")
    return "\n".join(lines)


def self_test():
    """造一段能被一个因子预测、另一个因子是噪音的小时数据，确认增量 R² 方向对。"""
    rng = np.random.default_rng(7)
    n = 24 * 80
    idx = pd.date_range("2025-08-01", periods=n, freq="1h", tz="UTC")
    z = rng.normal(size=n)
    log_y = -4.8 + 0.55 * z + rng.normal(scale=0.2, size=n)
    frame = pd.DataFrame({c: rng.normal(size=n) for c in M.VOL_FEATURES}, index=idx)
    frame["y4"] = np.exp(log_y)
    frame["skew_25d"] = z
    frame["imb_05"] = rng.normal(size=n)
    # 其余候选列不放，报告里应写「还没有」
    report = incremental_report(frame)
    by_key = {row["key"]: row for row in report["factors"]}
    skew = by_key["skew_25d"]
    noise = by_key["imb_05"]
    if skew["delta_r2"] is None or noise["delta_r2"] is None:
        raise SystemExit("自检没有跑出走步：" + json.dumps(report, ensure_ascii=False))
    if not (skew["delta_r2"] > 0.05 and skew["delta_r2"] > noise["delta_r2"] + 0.03):
        raise SystemExit(f"自检没通过：有用因子增量 {skew['delta_r2']}，噪音 {noise['delta_r2']}")
    # 对齐：11:00 整点的快照属于 10:00 这根
    snaps = [{"t": "2026-01-01T11:00:00Z", "spot": 100, "book": {"imb_05": 0.2, "imb_10": 0.1},
              "deribit": {"dvol": 40, "atm_iv": 41, "skew_25d": 2, "max_pain": 110},
              "cvd_15m": {"cvd": 1, "volume": 4}, "coinbase_premium_pct": 0.01,
              "exchanges": {"okx": {"funding": 0.0001, "basis_pct": 0.02}}}]
    hourly = snapshot_factors(snaps)
    if hourly.index[0] != pd.Timestamp("2026-01-01T10:00:00Z"):
        raise SystemExit(f"整点对齐错了：{hourly.index[0]}")
    if abs(hourly.iloc[0]["max_pain_dist"] - np.log(110 / 100)) > 1e-9:
        raise SystemExit("最大痛点距离算错了")
    print("自检通过：有信息的因子增量更高，快照能对齐到小时。")
    return report


def main():
    ap = argparse.ArgumentParser(description="因子快照的走步增量 R²")
    ap.add_argument("--snapshots", default="", help="jsonl 文件或 snapshots 目录")
    ap.add_argument("--klines", default="", help="5 分钟或 1 小时 K 线 csv。不给就联网下载")
    ap.add_argument("--dvol", default="", help="可选，DVOL csv（列 t,c 或 t,o,h,l,c）")
    ap.add_argument("--out", default="", help="把报告写成 JSON")
    ap.add_argument("--self-test", action="store_true")
    args = ap.parse_args()
    if args.self_test:
        self_test()
        return
    if not args.snapshots:
        ap.error("要给 --snapshots，或先跑 --self-test")
    snaps = load_snapshots(args.snapshots)
    if not snaps:
        print("快照是空的。等 factor-history 分支里有 snapshots/*.jsonl 再跑。")
        return
    times = [pd.Timestamp(s["t"]) for s in snaps if s.get("t")]
    span_days = (max(times) - min(times)).total_seconds() / 86400 if len(times) > 1 else 0
    print(f"读到 {len(snaps)} 条快照，时间跨度约 {span_days:.1f} 天。")
    if args.klines:
        klines = load_klines(args.klines)
    else:
        import vol_ruler
        days = max(int(span_days) + 10, 40)
        print(f"没有本地 K 线，向币安公开行情要最近 {days} 天。")
        klines = pd.DataFrame(vol_ruler.fetch_klines(days))
        klines = klines.iloc[:, :11]
        klines.columns = ["t", "o", "h", "l", "c", "v", "ct", "qv", "n", "tbv", "tbqv"]
    dvol = None
    if args.dvol:
        dvol = pd.read_csv(args.dvol)
    frame = build_joined(snaps, klines, dvol)
    report = incremental_report(frame)
    text = format_report(report)
    print(text)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            json.dump(report, f, ensure_ascii=False, indent=1)
        print("已写入", args.out)


if __name__ == "__main__":
    main()
