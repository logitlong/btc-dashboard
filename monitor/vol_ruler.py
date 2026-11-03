"""波动尺子：拉取公开行情，拟合 / 预测未来 1 小时和 4 小时振幅，写成 vol_ruler.json。

后台每 15 分钟预测一次；距离上次拟合超过 VOL_REFIT_HOURS（默认 20 小时）就重做走步检验并更新权重。
拟合结果另存在 vol_model.json，由状态分支带到下一段，避免每小时都重抓 90 天。

  python3 vol_ruler.py --once          # 预测；模型过期就先拟合
  python3 vol_ruler.py --once --refit  # 强制重新拟合

环境变量：VOL_LOOKBACK_DAYS（默认 90）、VOL_PREDICT_EVERY（秒，默认 900）、VOL_REFIT_HOURS（默认 20）、OUT_DIR。
"""
import json
import os
import time
import urllib.request
from datetime import datetime, timezone

import pandas as pd

import vol_model as M

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.environ.get("OUT_DIR", os.path.join(HERE, "out"))
VISION = "https://data-api.binance.vision/api/v3/klines"
DERIBIT = "https://www.deribit.com/api/v2/public/get_volatility_index_data"
UA = {"User-Agent": "btc-monitor/1.0"}


def _get(url, timeout=20, tries=3):
    last = None
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read().decode())
        except Exception as ex:
            last = ex
            time.sleep(1.2 * (i + 1))
    raise last


def fetch_klines(days):
    end = int(time.time() * 1000)
    start = end - int(days * 86400 * 1000)
    rows, s = [], start
    while s < end:
        url = f"{VISION}?symbol=BTCUSDT&interval=5m&startTime={s}&limit=1000"
        batch = _get(url)
        if not batch:
            break
        rows.extend(batch)
        s = int(batch[-1][0]) + 1
        if len(batch) < 1000:
            break
    return rows


def fetch_dvol(days):
    end = int(time.time() * 1000)
    start = end - int(days * 86400 * 1000)
    rows, s = [], start
    while s < end:
        e = min(s + 1000 * 3600 * 1000, end)
        url = f"{DERIBIT}?currency=BTC&resolution=3600&start_timestamp={s}&end_timestamp={e}"
        data = _get(url)["result"]["data"]
        rows.extend(data)
        if e >= end:
            break
        s = e
    return rows


def _iso(ts=None):
    dt = datetime.now(timezone.utc) if ts is None else datetime.fromtimestamp(ts, timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _horizon_block(pack, metrics, frame, edges):
    when, log_pred, row = M.predict_latest(pack, frame)
    pct = M.log_range_pct(log_pred)
    q = M.assign_quintile(log_pred, edges)
    eq, check = M.equation_rows(pack, row, log_pred)
    dvol_level = row.get("dvol_level")
    return {
        "asof": str(when),
        "pred_log": round(log_pred, 6),
        "pred_pct": round(pct, 4),
        "quintile": q,
        "quintile_label": M.Q_LABELS.get(q, ""),
        "intercept": round(float(pack["intercept"]), 6),
        "weights": pack["weights"],
        "weights_rounded": M.round_weights(pack, 3),
        "mu": pack["mu"],
        "sd": pack["sd"],
        "features": pack["features"],
        "equation": eq,
        "pred_check": check,
        "n_train": pack["n_train"],
        "train_start": pack["train_start"],
        "train_end": pack["train_end"],
        "context": {
            "dvol": None if dvol_level is None or not pd.notna(dvol_level) else round(float(dvol_level), 2),
            "last_hour_range_pct": None if not pd.notna(row.get("hour_range_pct")) else round(float(row["hour_range_pct"]), 4),
            "close": None if not pd.notna(row.get("close")) else round(float(row["close"]), 2),
            "us_session_next": bool(row.get("us_session_next", 0) >= 0.5),
            "weekend": bool(row.get("weekend", 0) >= 0.5),
        },
        "oos": {k: metrics[k] for k in ("oos_start", "oos_end", "n", "r2", "quintiles", "median_actual_pct", "pred_col")},
    }


def fit_frame(hourly, log=print):
    """对同一张小时表做 4h / 1h 的走步和全样本拟合。含 DVOL 与不含 DVOL 两套权重都留下。"""
    frame = M.features_from_hourly(hourly)
    has_dvol = bool(frame["dvol"].notna().sum() > 24 * 7)
    out_models = {"with_dvol": has_dvol, "fit_at": _iso(), "fit_unix": time.time(), "alpha": M.ALPHA}
    for key, hours, target in (("h4", 4, "y4"), ("h1", 1, "y1")):
        oos, metrics = M.walk_forward(frame, horizon_hours=hours, target=target, with_dvol=has_dvol)
        pack = M.fit_production(frame, hours, target, with_dvol=has_dvol)
        pack_base = pack if not has_dvol else M.fit_production(frame, hours, target, with_dvol=False)
        edges_base = M._quintile_edges(oos["full"]) if len(oos) and "full" in oos.columns else []
        out_models[key] = {
            "pack": pack,
            "pack_base": pack_base,
            "metrics": metrics,
            "edges": metrics.get("q_edges_log") or [],
            "edges_base": edges_base,
        }
        r = metrics.get("r2") or {}
        log(f"波动尺子 {key} 样本外 R² naive={r.get('naive')} har={r.get('har')} full={r.get('full')} full_dvol={r.get('full_dvol')} n={metrics.get('n')}")
    return frame, out_models


def _select_pack(block, frame):
    """DVOL 超过 6 小时没有新值时，改用同一轮拟合里不含 DVOL 的权重。"""
    pack = block["pack"]
    edges = block.get("edges") or []
    if "dvol" not in pack["features"]:
        return pack, edges, True
    ready = frame.dropna(subset=pack["features"])
    fresh = False
    if not ready.empty:
        when = ready.index[-1]
        if when.tzinfo is None:
            when = when.tz_localize("UTC")
        fresh = pd.Timestamp.now(tz="UTC") - when <= pd.Timedelta(hours=6)
    if fresh:
        return pack, edges, True
    return block.get("pack_base") or pack, block.get("edges_base") or edges, False


def predict_payload(frame, models, price=None):
    used = {}
    blocks = {}
    for key in ("h4", "h1"):
        pack, edges, used_dvol = _select_pack(models[key], frame)
        used[key] = used_dvol
        block = _horizon_block(pack, models[key]["metrics"], frame, edges)
        block["used_dvol"] = used_dvol
        blocks[key] = block
    h4, h1 = blocks["h4"], blocks["h1"]
    px = price if price is not None else (h4.get("context") or {}).get("close")
    payload = {
        "updated": _iso(),
        "refit_at": models.get("fit_at"),
        "lookback_days": models.get("lookback_days"),
        "alpha": M.ALPHA,
        "definition": "振幅% = ln(区间最高/最低) × 100，和 (最高−最低)/价格 几乎相同",
        "with_dvol": bool(models.get("with_dvol")),
        "used_dvol": bool(used.get("h4")),
        "price": None if px is None else round(float(px), 2),
        "h4": h4,
        "h1": h1,
        "practical": M.practical_block(h4["pred_pct"], px),
        "summary_zh": M.summarize_zh(h4, h1),
        "info_only": "只提供信息，不下单，也不是投资建议。",
    }
    if models.get("with_dvol") and not used.get("h4"):
        payload["dvol_note"] = "最近 6 小时没有新的 DVOL，这次预测用的是同一轮拟合里不含 DVOL 的权重。样本外 R² 仍是含 DVOL 的走步结果。"
    return payload


def _strip_models(models):
    """vol_model.json 只留拟合和样本外，不留某一时刻的预测。"""
    keep = {"fit_at": models.get("fit_at"), "fit_unix": models.get("fit_unix"),
            "with_dvol": models.get("with_dvol"), "alpha": models.get("alpha"),
            "lookback_days": models.get("lookback_days")}
    for key in ("h4", "h1"):
        block = models[key]
        keep[key] = {
            "pack": block["pack"],
            "pack_base": block.get("pack_base"),
            "metrics": block["metrics"],
            "edges": block["edges"],
            "edges_base": block.get("edges_base") or [],
        }
    return keep


class VolRuler:
    def __init__(self, out_dir, log=print):
        self.out = out_dir
        self.log = log
        self.every = int(os.environ.get("VOL_PREDICT_EVERY", "900"))
        self.refit_hours = float(os.environ.get("VOL_REFIT_HOURS", "20"))
        self.days = int(os.environ.get("VOL_LOOKBACK_DAYS", "90"))
        self.model_path = os.path.join(out_dir, "vol_model.json")
        self.out_path = os.path.join(out_dir, "vol_ruler.json")
        self.next_at = 0

    def step(self, force_refit=False):
        if time.time() < self.next_at:
            return
        try:
            self.run(force_refit=force_refit)
            self.next_at = time.time() + self.every
        except Exception as ex:
            self.log("波动尺子失败，5 分钟后再试:", ex)
            self.next_at = time.time() + 300

    def load_model(self):
        if not os.path.exists(self.model_path):
            return None
        try:
            return json.load(open(self.model_path, encoding="utf-8"))
        except Exception as ex:
            self.log("波动尺子模型读不了，准备重拟合:", ex)
            return None

    def model_fresh(self, model):
        if not model or not model.get("fit_unix"):
            return False
        return time.time() - float(model["fit_unix"]) < self.refit_hours * 3600

    def refit(self):
        self.log(f"波动尺子开始拟合：最近 {self.days} 天 5 分钟 K 线 + DVOL，并做走步检验")
        rows = fetch_klines(self.days)
        dvol = fetch_dvol(self.days)
        hourly = M.hourly_from_klines(rows)
        hourly = M.attach_dvol(hourly, dvol)
        frame, models = fit_frame(hourly, self.log)
        models["lookback_days"] = self.days
        os.makedirs(self.out, exist_ok=True)
        _dump(self.model_path, _strip_models(models))
        return frame, models

    def run(self, force_refit=False):
        os.makedirs(self.out, exist_ok=True)
        model = None if force_refit else self.load_model()
        if force_refit or not self.model_fresh(model):
            frame, models = self.refit()
        else:
            self.log("波动尺子用已有拟合做预测（未过期）")
            rows = fetch_klines(10)
            dvol = fetch_dvol(10)
            hourly = M.hourly_from_klines(rows)
            hourly = M.attach_dvol(hourly, dvol)
            frame = M.features_from_hourly(hourly)
            models = model
        # 预测时如果 DVOL 断了，已保存的模型仍含 dvol 因子，predict_latest 会因为缺值退回到更早的完整行。
        # 若最近 6 小时都没有 DVOL，而模型又需要它，就改用去掉 DVOL 的临时预测并注明。
        payload = predict_payload(frame, models)
        _dump(self.out_path, payload)
        h4 = payload["h4"]
        self.log(f"波动尺子 4h {h4['pred_pct']:.2f}% 档位 {h4['quintile']}（{h4['quintile_label']}）"
                 f" 1h {payload['h1']['pred_pct']:.2f}% 已写入 vol_ruler.json")
        return payload


def _dump(path, obj):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="拟合或更新波动尺子")
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--refit", action="store_true", help="强制重新走步并拟合")
    ap.add_argument("--out", default=OUT)
    ap.add_argument("--save-report", default="", help="把 vol_ruler.json 再复制到这个路径")
    args = ap.parse_args()
    ruler = VolRuler(args.out, print)
    payload = ruler.run(force_refit=args.refit or not os.path.exists(ruler.model_path))
    print(payload["summary_zh"])
    if args.save_report:
        os.makedirs(os.path.dirname(os.path.abspath(args.save_report)), exist_ok=True)
        _dump(args.save_report, payload)
        print("报告已写入", args.save_report)
