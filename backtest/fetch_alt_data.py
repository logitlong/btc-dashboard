"""下载持仓 / 多空比、盘口深度、Deribit 期权成交的历史，给 alt_data_study.py 用。

  metrics   币安合约每 5 分钟持仓量、大户 / 全部账户多空比、主动买卖比（2020-09 起）
  bookdepth 币安合约盘口 ±1%…±5% 挂单金额，约 30 秒一次，这里每 5 分钟留最后一次（2023-01 起）
  deribit   Deribit BTC 期权成交，每 4 小时取收盘前 1 小时的成交（2022-01 起）

都存到 backtest/data/history/，已下载的跳过。用法：python fetch_alt_data.py metrics|bookdepth|deribit
"""
import csv
import datetime as dt
import io
import json
import os
import sys
import time
import urllib.request
import zipfile
from concurrent.futures import ThreadPoolExecutor

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "history")
BIN = "https://data.binance.vision/data/futures/um/daily"
END = dt.date(2026, 10, 31)


def get(url, tries=4):
    for k in range(tries):
        try:
            with urllib.request.urlopen(url, timeout=60) as r:
                return r.read()
        except urllib.error.HTTPError as ex:
            if ex.code == 404:
                return None
            time.sleep(2 * (k + 1))
        except Exception:
            time.sleep(2 * (k + 1))
    return None


def days(start):
    d = start
    while d <= END:
        yield d
        d += dt.timedelta(days=1)


def one_metrics(d):
    out = os.path.join(ROOT, "metrics", f"{d}.csv")
    if os.path.exists(out):
        return
    raw = get(f"{BIN}/metrics/BTCUSDT/BTCUSDT-metrics-{d}.zip")
    if raw is None:
        open(out, "w").close()
        return
    z = zipfile.ZipFile(io.BytesIO(raw))
    open(out, "wb").write(z.read(z.namelist()[0]))


def one_book(d):
    out = os.path.join(ROOT, "bookdepth", f"{d}.csv")
    if os.path.exists(out):
        return
    raw = get(f"{BIN}/bookDepth/BTCUSDT/BTCUSDT-bookDepth-{d}.zip")
    if raw is None:
        open(out, "w").close()
        return
    z = zipfile.ZipFile(io.BytesIO(raw))
    keep = {}  # 5 分钟桶 -> {百分比: 金额}，同一桶留最后一次快照
    with z.open(z.namelist()[0]) as f:
        rd = csv.reader(io.TextIOWrapper(f))
        next(rd, None)
        for ts, pct, _depth, notional in rd:
            if not ts[:1].isdigit():
                continue
            t = dt.datetime.fromisoformat(ts)
            b = t.replace(minute=t.minute - t.minute % 5, second=0)
            snap = keep.setdefault(b, {})
            if snap.get("_ts", "") != ts:
                if snap.get("_ts", "") > ts:
                    continue
                snap.clear()
                snap["_ts"] = ts
            snap[pct] = notional
    with open(out, "w") as f:
        f.write("t,pct,notional\n")
        for b, snap in sorted(keep.items()):
            for pct, v in snap.items():
                if pct != "_ts":
                    f.write(f"{b:%Y-%m-%d %H:%M},{pct},{v}\n")


def one_deribit(t_end):
    out = os.path.join(ROOT, "deribit_opt", f"{t_end}.json")
    if os.path.exists(out):
        return
    e = t_end * 1000
    url = ("https://history.deribit.com/api/v2/public/get_last_trades_by_currency_and_time?currency=BTC&kind=option"
           f"&start_timestamp={e - 3600_000}&end_timestamp={e}&count=1000&sorting=desc")
    raw = get(url)
    if raw is None:
        return
    tr = json.loads(raw)["result"]["trades"]
    keep = [[x["timestamp"], x["instrument_name"], x["mark_price"], x["price"], x["iv"], x["index_price"],
             x["direction"], x["amount"]] for x in tr]
    json.dump(keep, open(out, "w"))


def main(kind):
    if kind == "metrics":
        jobs, fn, workers = list(days(dt.date(2020, 9, 1))), one_metrics, 8
    elif kind == "bookdepth":
        jobs, fn, workers = list(days(dt.date(2023, 1, 1))), one_book, 6
    elif kind == "deribit":
        t0 = int(dt.datetime(2022, 1, 1, tzinfo=dt.timezone.utc).timestamp())
        t1 = int(dt.datetime(2026, 10, 28, tzinfo=dt.timezone.utc).timestamp())
        jobs, fn, workers = list(range(t0, t1, 4 * 3600)), one_deribit, 10
    else:
        raise SystemExit(__doc__)
    done = 0
    with ThreadPoolExecutor(workers) as ex:
        for _ in ex.map(fn, jobs):
            done += 1
            if done % 200 == 0:
                print(kind, done, "/", len(jobs), flush=True)
    print(kind, "done", len(jobs))


if __name__ == "__main__":
    main(sys.argv[1])
