"""因子快照：一个来源挂了不能弄丢其他来源；盘口、最大痛点、追加写入。"""
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import factor_snap as F


def test_imbalance_band():
    bids = [(100, 2), (99, 5)]
    asks = [(101, 1), (102, 8)]
    imb, truncated = F.book_imbalance(bids, asks, 0.01)
    # 中间价 100.5，±1% 大约 99.5–101.5：买只算 100 的 2，卖只算 101 的 1
    assert abs(imb - (2 - 1) / 3) < 1e-9
    assert not truncated  # 最远一档已经盖过 ±1%
    thin, thin_tr = F.book_imbalance([(100, 2)], [(101, 1)], 0.01)
    assert thin_tr and abs(thin - (2 - 1) / 3) < 1e-9


def test_max_pain_and_skew_side():
    calls = [(100, 10), (110, 10)]
    puts = [(90, 10), (100, 10)]
    assert F.max_pain(calls, puts) == 100
    # 25 delta 看涨在现货上方，看跌在下方
    k_call = F.strike_for_d1(100, -F.NINV_25, 0.5, 7 / 365)
    k_put = F.strike_for_d1(100, F.NINV_25, 0.5, 7 / 365)
    assert k_call > 100 > k_put


def test_surface_from_names():
    now = __import__("datetime").datetime(2026, 11, 3, tzinfo=__import__("datetime").timezone.utc)
    rows = []
    # 3 天后到期，两档
    for strike, kind, iv, oi in (
        (100, "C", 40, 5), (100, "P", 42, 5),
        (110, "C", 45, 1), (90, "P", 50, 1),
    ):
        rows.append({
            "instrument_name": f"BTC-6NOV26-{strike}-{kind}",
            "open_interest": oi, "mark_iv": iv, "underlying_price": 100,
        })
    surface = F.deribit_surface(rows, now)
    assert surface["expiry"] == "2026-11-06"
    assert abs(surface["atm_iv"] - 41) < 1e-6  # 100 的看涨 40、看跌 42
    assert surface["max_pain"] == 100
    assert surface["oi_by_expiry"][0]["call"] == 6
    assert surface["skew_25d"] is not None


def test_one_source_fails_others_remain():
    def get(url, body=None, timeout=15):
        if "deribit" in url:
            raise F.SnapError("HTTP 500 www.deribit.com")
        if "fapi.binance.com" in url:
            raise F.SnapError("HTTP 451 fapi.binance.com")
        if "bybit" in url:
            raise F.SnapError("HTTP 403 api.bybit.com")
        if url.endswith("/depth?symbol=BTCUSDT&limit=1000") or "depth?symbol=BTCUSDT" in url:
            return {"bids": [["100", "2"]], "asks": [["101", "3"]]}
        if "klines" in url:
            return [[0, "1", "1", "1", "1", "4", 0, "0", 1, "3", "0"]]
        if "okx.com" in url and "ticker" in url and "SWAP" in url:
            return {"data": [{"bidPx": "100", "askPx": "102", "last": "101"}]}
        if "okx.com" in url and "ticker" in url:
            return {"data": [{"bidPx": "100", "askPx": "100", "last": "100"}]}
        if "funding-rate" in url:
            return {"data": [{"fundingRate": "0.0001"}]}
        if "bitget.com" in url and "mix" in url:
            return {"data": [{"bidPr": "100", "askPr": "100", "lastPr": "100", "fundingRate": "0.0002",
                              "markPrice": "100", "indexPrice": "100"}]}
        if "bitget.com" in url:
            return {"data": [{"bidPr": "100", "askPr": "101", "lastPr": "100"}]}
        if "gateio" in url and "futures" in url:
            return [{"last": "100", "funding_rate": "0.0001", "mark_price": "100", "index_price": "99"}]
        if "gateio" in url:
            return [{"last": "100", "highest_bid": "99", "lowest_ask": "101"}]
        if "hyperliquid" in url:
            return [{"universe": [{"name": "BTC"}]}, [{"funding": "0.00001", "markPx": "101", "oraclePx": "100"}]]
        if "BTC-USD" in url:
            return {"bid": "101", "ask": "103", "price": "102"}
        if "USDT-USD" in url:
            return {"bid": "1", "ask": "1", "price": "1"}
        raise AssertionError(url)

    snap = F.collect_snapshot(get, now=__import__("datetime").datetime(2026, 11, 3, 11, 47, tzinfo=__import__("datetime").timezone.utc))
    assert snap["t"] == "2026-11-03T11:47:00Z"
    assert "error" in snap["deribit"]
    assert "error" in snap["exchanges"]["binance_perp"]
    assert "error" in snap["exchanges"]["bybit"]
    assert snap["exchanges"]["okx"]["funding"] == 0.0001
    assert snap["book"]["mid"] == 100.5
    # 成交量 4，主动买 3，主动卖 1，差值 2
    assert snap["cvd_15m"]["buy"] == 3
    assert snap["cvd_15m"]["cvd"] == 2
    assert snap["coinbase_premium_pct"] is not None
    assert any(e.startswith("deribit") for e in snap["errors"])
    # 失败的来源没有把整行弄丢
    line = F.snapshot_line(snap)
    assert json.loads(line)["exchanges"]["hyperliquid"]["basis_kind"] == "mark_vs_oracle"


class MemBranch:
    """不联网的分支：文件字典。"""

    def __init__(self):
        self.files = {}
        self.commits = 0

    def branch_exists(self):
        return self.commits > 0

    def ensure_docs(self, files):
        if not self.branch_exists():
            return
        for path, text in files.items():
            self.files.setdefault(path, text)

    def append_line(self, path, line, message, seed_files=None):
        line = line if line.endswith("\n") else line + "\n"
        if not self.branch_exists():
            self.files.update(seed_files or {})
            self.files[path] = ""
            self.commits += 1
        text = self.files.get(path)
        if text is None:
            self.files[path] = line
            self.commits += 1
            return True
        if line in text:
            return False
        self.files[path] = text + line
        self.commits += 1
        return True


def test_append_only_partition_and_dedup():
    with tempfile.TemporaryDirectory() as d:
        rec = F.FactorRecorder(d, log=lambda *a: None, client=MemBranch())
        a = {"t": "2026-11-03T11:45:00Z", "spot": 1}
        b = {"t": "2026-11-03T12:00:00Z", "spot": 2}
        rec.run_once(a)
        rec.run_once(a)  # 同一条再来一次
        rec.run_once(b)
        text = rec.client.files["snapshots/2026-11-03.jsonl"]
        lines = [ln for ln in text.splitlines() if ln]
        assert len(lines) == 2
        assert json.loads(lines[0])["spot"] == 1
        assert "schema.json" in rec.client.files
        # 队列清空了，没有把已推送的再留一份
        assert not os.path.exists(rec.queue_path)


def test_local_fallback_without_token():
    with tempfile.TemporaryDirectory() as d:
        rec = F.FactorRecorder(d, log=lambda *a: None, client=False)
        # client=False 不是 None，__init__ 不会自己去建 GitHub 客户端
        rec.client = None
        rec.run_once({"t": "2026-11-04T00:15:00Z", "spot": 3})
        path = os.path.join(d, "factor-history", "2026-11-04.jsonl")
        assert os.path.exists(path)
        assert json.loads(open(path, encoding="utf-8").read())["spot"] == 3


if __name__ == "__main__":
    test_imbalance_band()
    test_max_pain_and_skew_side()
    test_surface_from_names()
    test_one_source_fails_others_remain()
    test_append_only_partition_and_dedup()
    test_local_fallback_without_token()
    print("ok")
