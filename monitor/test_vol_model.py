"""波动尺子：不联网。岭回归、走步、档位、强平距离、发布文件名。"""
import json
import os
import sys
import tempfile

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import vol_model as M
import publish


def _hourly(n=24 * 80, seed=1):
    rng = np.random.default_rng(seed)
    idx = pd.date_range("2025-06-01", periods=n, freq="1h", tz="UTC")
    # 波动有记忆，这样 HAR 的样本外 R² 应该是正的
    log_sig = np.zeros(n)
    for t in range(1, n):
        log_sig[t] = 0.8 * log_sig[t - 1] + rng.normal(0, 0.25)
    sig = np.exp(log_sig) * 0.004
    close = 100 * np.exp(np.cumsum(rng.normal(0, sig * 0.3)))
    span = close * sig
    hourly = pd.DataFrame({
        "open": close,
        "high": close + span,
        "low": close - span * 0.8,
        "close": close,
        "volume": rng.uniform(10, 20, n) * np.exp(log_sig),
        "dvol": 40 * np.exp(0.3 * log_sig),
    }, index=idx)
    hourly["high"] = np.maximum(hourly["high"], hourly[["open", "close"]].max(axis=1) * 1.0001)
    hourly["low"] = np.minimum(hourly["low"], hourly[["open", "close"]].min(axis=1) * 0.9999)
    return hourly


def test_ridge_matches_normal_equation():
    rng = np.random.default_rng(0)
    X = rng.normal(size=(50, 3))
    y = X @ np.array([1.5, -0.5, 0.2]) + 0.3 + rng.normal(scale=0.01, size=50)
    w, b = M.ridge_fit(X, y, alpha=1.0)
    pred = M.ridge_predict(X, w, b)
    # 手工：中心化后解 (X'X + I) w = X'y
    xc = X - X.mean(0)
    yc = y - y.mean()
    w2 = np.linalg.solve(xc.T @ xc + np.eye(3), xc.T @ yc)
    assert np.allclose(w, w2)
    assert abs(b - (y.mean() - X.mean(0) @ w)) < 1e-9
    assert M.r2_score(y, pred) > 0.9


def test_walk_forward_positive_and_quintile():
    frame = M.features_from_hourly(_hourly())
    oos, metrics = M.walk_forward(frame, horizon_hours=4, target="y4", with_dvol=True)
    assert metrics["n"] > 100
    assert metrics["r2"]["har"] > 0.05
    assert metrics["r2"]["full_dvol"] > metrics["r2"]["naive"]
    assert metrics["quintiles"]
    assert {q["q"] for q in metrics["quintiles"]} <= {1, 2, 3, 4, 5}
    # 档位越高，预测中位越大
    meds = [q["pred_med_pct"] for q in metrics["quintiles"]]
    assert meds == sorted(meds)
    pack = M.fit_production(frame, 4, "y4", with_dvol=True)
    when, log_pred, row = M.predict_latest(pack, frame)
    q = M.assign_quintile(log_pred, metrics["q_edges_log"])
    assert q in (1, 2, 3, 4, 5)
    eq, check = M.equation_rows(pack, row, log_pred)
    assert abs(check - log_pred) < 1e-6
    assert eq[0]["name"] == "过去 1 小时振幅"
    assert set(pack["weights"]) == set(M.VOL_FEATURES)
    # 权重来自拟合，不是空的
    assert any(abs(v) > 1e-6 for v in pack["weights"].values())


def test_future_range_does_not_include_current_bar():
    idx = pd.date_range("2026-01-01", periods=6, freq="1h", tz="UTC")
    high = pd.Series([1, 10, 3, 4, 8, 2], index=idx, dtype=float)
    low = pd.Series([1, 1, 1, 1, 1, 1], index=idx, dtype=float)
    y = M.future_log_range(high, low, 2)
    # t0 的未来两根是 high 10 和 3，ln(10/1)
    assert abs(y.iloc[0] - np.log(10)) < 1e-9
    assert np.isnan(y.iloc[-1]) and np.isnan(y.iloc[-2])


def test_leverage_table():
    # 1% 的参考距离：20 倍碰不到，100 倍会碰到；一半缓冲大约到 41 倍
    block = M.practical_block(1.0, 77000)
    by = {row["lev"]: row for row in block["leverage"]}
    assert by[20]["liq_distance_pct"] == round((1 / 20 - 0.004) * 100, 4)
    assert by[20]["stop_below_half_liq"] and not by[20]["normal_move_reaches_liq"]
    assert by[100]["normal_move_reaches_liq"]
    assert not by[50]["stop_below_half_liq"]
    assert block["max_leverage_stop_below_half_liq"] == 41
    assert abs(block["stop_usd"] - 770) < 1e-6
    assert "不是下单" in block["basis"]


def test_publisher_lists_vol_ruler():
    with tempfile.TemporaryDirectory() as d:
        for name in ("status.json", "vol_ruler.json"):
            with open(os.path.join(d, name), "w", encoding="utf-8") as f:
                f.write("{}")
        os.environ.pop("GITHUB_REPO", None)
        os.environ.pop("GITHUB_TOKEN", None)
        pub = publish.Publisher(d, log=lambda *a: None)
        files = pub.files()
        assert "vol_ruler.json" in files
        assert "status.json" in files
        assert "rules_spec.json" in files


def test_oos_r2_rewards_real_factor():
    rng = np.random.default_rng(3)
    n = 24 * 70
    idx = pd.date_range("2025-01-01", periods=n, freq="1h", tz="UTC")
    z = rng.normal(size=n)
    frame = pd.DataFrame({c: rng.normal(size=n) for c in M.FULL_FEATURES}, index=idx)
    frame["y4"] = np.exp(-4.7 + 0.5 * z + rng.normal(scale=0.15, size=n))
    frame["z"] = z
    base, n0 = M.oos_r2(frame, M.FULL_FEATURES, "y4", 4)
    plus, n1 = M.oos_r2(frame, M.FULL_FEATURES + ["z"], "y4", 4)
    assert n0 > 100 and n1 == n0
    assert plus > base + 0.05


if __name__ == "__main__":
    test_ridge_matches_normal_equation()
    test_future_range_does_not_include_current_bar()
    test_walk_forward_positive_and_quintile()
    test_leverage_table()
    test_publisher_lists_vol_ruler()
    test_oos_r2_rewards_real_factor()
    print("ok")
