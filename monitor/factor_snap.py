"""候选因子快照。每 15 分钟抓一次公开数据，追加到 factor-history 分支。

一个来源失败只在这一行里记错误，不影响其他来源，也不影响监控主循环。
币安合约（fapi）在云服务器上会 451，Bybit 会 403，仍然各试一次，失败就跳过。

分支是追加的：snapshots/年-月-日.jsonl 每天一个文件。不要用 data 分支那种
「无父提交、整支覆盖」的写法，否则历史会被抹掉。

没有 GITHUB_REPO / GITHUB_TOKEN 时只写到本机 out/factor-history/。
"""
import base64
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

import exchanges as EX

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.environ.get("OUT_DIR", os.path.join(HERE, "out"))
UA = {"User-Agent": "btc-monitor/1.0", "Accept": "application/json"}
API = "https://api.github.com"

VISION = "https://data-api.binance.vision"
DERIBIT = "https://www.deribit.com/api/v2/public"

MONTHS = {m: i for i, m in enumerate(
    ["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"], 1)}
# 标准正态 25 分位的绝对值，用来近似 25 delta 行权价
NINV_25 = 0.67448975

SCHEMA = {
    "branch": "factor-history",
    "cadence": "每 15 分钟追加一行，按 UTC 日期分文件 snapshots/YYYY-MM-DD.jsonl",
    "append_only": True,
    "note": "单个来源失败时该字段是 {error: ...}，其他字段仍在。币安合约、Bybit 在部分云主机上会被拒绝，失败是预期情况。",
    "fields": {
        "t": "UTC 时间，精确到分钟",
        "spot": "币安现货中间价",
        "deribit.dvol": "Deribit BTC DVOL 最近一根 1 小时收盘",
        "deribit.atm_iv": "所选到期日、行权价最接近标的的看涨/看跌 mark_iv 平均（Deribit 的点位，45 表示 45%）",
        "deribit.skew_25d": "25 delta 看跌 IV 减 25 delta 看涨 IV（点）。行权价用 ATM IV 近似，不是交易所直接给的 delta",
        "deribit.max_pain": "所选到期日的最大痛点行权价（美元）",
        "deribit.oi_by_expiry": "每个到期日的看涨/看跌持仓，单位是 Deribit book summary 的 open_interest（BTC 期权）",
        "book.imb_05": "现货盘口 ±0.5% 的深度失衡 (买量-卖量)/(买量+卖量)。请求 5000 档；truncated_05 为 true 表示档位没盖满这条带",
        "book.imb_10": "现货盘口 ±1% 的深度失衡。truncated_10 为 true 时带内数量不完整",
        "cvd_15m": "最近 15 根 1 分钟 K 线的主动买、主动卖和差值（币安现货，base 数量）",
        "exchanges.*.funding": "资金费率，原样保存。OKX / Bitget / Gate 约 8 小时，Hyperliquid 约 1 小时，不能直接横比",
        "exchanges.*.basis_pct": "永续相对现货的基差（%）。Hyperliquid 没有现货中间价时用标记价相对预言机",
        "coinbase_premium_pct": "Coinbase BTC-USD / (币安现货 × Coinbase USDT-USD) − 1，单位 %",
    },
}


class SnapError(RuntimeError):
    pass


def http_json(url, timeout=15, body=None, method=None):
    data = None if body is None else (body if isinstance(body, bytes) else json.dumps(body).encode())
    headers = dict(UA)
    if data is not None:
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, method=method or ("POST" if data else "GET"), headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as ex:
        raise SnapError(f"HTTP {ex.code} {urllib.parse.urlparse(url).netloc}") from ex


def _f(v):
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    if x != x:  # NaN
        return None
    return x


def _r(v, n=6):
    x = _f(v)
    return None if x is None else round(x, n)


def parse_instrument(name):
    parts = str(name).split("-")
    if len(parts) != 4:
        return None
    exp, strike, kind = parts[1], parts[2], parts[3]
    if len(exp) < 6 or kind not in ("C", "P"):
        return None
    mon = MONTHS.get(exp[-5:-2].upper())
    if not mon:
        return None
    try:
        day = int(exp[:-5])
        year = 2000 + int(exp[-2:])
        strike_v = float(strike)
    except ValueError:
        return None
    return datetime(year, mon, day, tzinfo=timezone.utc), strike_v, kind


def _years_to(expiry, now):
    return max((expiry - now).total_seconds(), 0.0) / (365.25 * 86400)


def strike_for_d1(forward, d1, sigma, t_years):
    if forward <= 0 or sigma <= 0 or t_years <= 0:
        return None
    import math
    return forward * math.exp(-d1 * sigma * math.sqrt(t_years) + 0.5 * sigma * sigma * t_years)


def max_pain(calls, puts):
    """calls/puts: [(strike, oi), ...]。返回让买方最不值钱的行权价。"""
    strikes = sorted({k for k, _ in calls + puts})
    if not strikes:
        return None
    best_k, best_p = None, None
    for k in strikes:
        pain = 0.0
        for strike, oi in calls:
            pain += max(k - strike, 0.0) * oi
        for strike, oi in puts:
            pain += max(strike - k, 0.0) * oi
        if best_p is None or pain < best_p:
            best_k, best_p = k, pain
    return best_k


def _nearest(strikes, target):
    if target is None or not strikes:
        return None
    return min(strikes, key=lambda k: abs(k - target))


def deribit_surface(book_rows, now=None):
    """从 get_book_summary_by_currency 的 result 里抽出 ATM、偏斜、最大痛点和持仓。"""
    now = now or datetime.now(timezone.utc)
    parsed = []
    for row in book_rows or []:
        info = parse_instrument(row.get("instrument_name", ""))
        if not info:
            continue
        expiry, strike, kind = info
        oi = _f(row.get("open_interest")) or 0.0
        iv = _f(row.get("mark_iv"))
        und = _f(row.get("underlying_price"))
        parsed.append((expiry, strike, kind, oi, iv, und))
    by_exp = {}
    for expiry, strike, kind, oi, iv, und in parsed:
        bucket = by_exp.setdefault(expiry, {"call": {}, "put": {}, "und": []})
        bucket[("call" if kind == "C" else "put")][strike] = (oi, iv)
        if und:
            bucket["und"].append(und)
    oi_by_expiry = []
    for expiry, bucket in sorted(by_exp.items(), key=lambda kv: kv[0]):
        call_oi = sum(v[0] for v in bucket["call"].values())
        put_oi = sum(v[0] for v in bucket["put"].values())
        if call_oi + put_oi <= 0:
            continue
        oi_by_expiry.append({
            "expiry": expiry.strftime("%Y-%m-%d"),
            "call": round(call_oi, 2),
            "put": round(put_oi, 2),
        })
    # 1 到 14 天里持仓最大的到期日；没有就用最近一个还没到期的
    window = []
    for expiry, bucket in by_exp.items():
        days = (expiry - now).total_seconds() / 86400
        oi = sum(v[0] for v in bucket["call"].values()) + sum(v[0] for v in bucket["put"].values())
        if days > 0.25:
            window.append((days, oi, expiry))
    chosen = None
    band = [x for x in window if 1 <= x[0] <= 14]
    pool = band or window
    if pool:
        pool.sort(key=lambda x: (-x[1], x[0]))
        chosen = pool[0][2]
    if chosen is None:
        return {"oi_by_expiry": oi_by_expiry}
    bucket = by_exp[chosen]
    und = sorted(bucket["und"])[len(bucket["und"]) // 2] if bucket["und"] else None
    call_strikes = list(bucket["call"])
    put_strikes = list(bucket["put"])
    atm_k = _nearest(sorted(set(call_strikes) | set(put_strikes)), und)
    ivs = []
    for side, strikes in (("call", call_strikes), ("put", put_strikes)):
        if atm_k in bucket[side] and bucket[side][atm_k][1]:
            ivs.append(bucket[side][atm_k][1])
    atm = sum(ivs) / len(ivs) if ivs else None
    t_years = _years_to(chosen, now)
    sigma = (atm / 100.0) if atm else None
    k_put = strike_for_d1(und, NINV_25, sigma, t_years) if und and sigma else None
    k_call = strike_for_d1(und, -NINV_25, sigma, t_years) if und and sigma else None
    put_k = _nearest(put_strikes, k_put)
    call_k = _nearest(call_strikes, k_call)
    put_iv = bucket["put"].get(put_k, (None, None))[1] if put_k is not None else None
    call_iv = bucket["call"].get(call_k, (None, None))[1] if call_k is not None else None
    skew = None if put_iv is None or call_iv is None else put_iv - call_iv
    calls = [(k, v[0]) for k, v in bucket["call"].items()]
    puts = [(k, v[0]) for k, v in bucket["put"].items()]
    return {
        "expiry": chosen.strftime("%Y-%m-%d"),
        "dte_days": round((chosen - now).total_seconds() / 86400, 2),
        "underlying": _r(und, 2),
        "atm_iv": _r(atm, 4),
        "atm_strike": _r(atm_k, 2),
        "skew_25d": _r(skew, 4),
        "skew_put_strike": _r(put_k, 2),
        "skew_call_strike": _r(call_k, 2),
        "max_pain": _r(max_pain(calls, puts), 2),
        "oi_by_expiry": oi_by_expiry,
    }


def book_imbalance(bids, asks, band):
    """band=0.005 表示中间价上下 0.5%。返回失衡和是否被档位截断。"""
    bids = [(_f(p), _f(q)) for p, q in bids]
    asks = [(_f(p), _f(q)) for p, q in asks]
    bids = [(p, q) for p, q in bids if p and q]
    asks = [(p, q) for p, q in asks if p and q]
    if not bids or not asks:
        return None, False
    mid = (bids[0][0] + asks[0][0]) / 2.0
    lo, hi = mid * (1 - band), mid * (1 + band)
    b = sum(q for p, q in bids if p >= lo)
    a = sum(q for p, q in asks if p <= hi)
    truncated = bids[-1][0] > lo or asks[-1][0] < hi
    if b + a <= 0:
        return None, truncated
    return (b - a) / (b + a), truncated


def cvd_from_klines(rows):
    buy = sell = vol = 0.0
    n = 0
    for row in rows or []:
        if len(row) < 10:
            continue
        volume = _f(row[5]) or 0.0
        taker_buy = _f(row[9]) or 0.0
        buy += taker_buy
        sell += max(volume - taker_buy, 0.0)
        vol += volume
        n += 1
    if n == 0:
        return None
    return {"buy": round(buy, 6), "sell": round(sell, 6), "cvd": round(buy - sell, 6), "volume": round(vol, 6), "bars": n}


def _mid(bid, ask, last=None):
    b, a = _f(bid), _f(ask)
    if b and a and b > 0 and a > 0:
        return (b + a) / 2.0
    return _f(last)


def basis_pct(perp, spot):
    if not perp or not spot:
        return None
    return (perp / spot - 1.0) * 100.0


def collect_snapshot(get=http_json, now=None, ex_get=EX.client):
    """抓一次。get(url, body=None) 用于币安现货盘口 / K 线和 Deribit；各交易所永续和 Coinbase 走 ccxt（ex_get 取客户端）。
    任何一个来源抛异常都只影响它自己。"""
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    stamp = now.astimezone(timezone.utc).replace(second=0, microsecond=0)
    out = {"t": stamp.strftime("%Y-%m-%dT%H:%M:%SZ"), "t_ms": int(stamp.timestamp() * 1000)}
    errors = []

    def take(name, fn):
        try:
            out[name] = fn()
        except Exception as ex:
            msg = str(ex)[:180]
            out[name] = {"error": msg}
            errors.append(f"{name}: {msg}")

    def spot_book():
        # 1000 档只覆盖大约十几美元，到不了 ±0.5%。5000 档在 BTC 上大约能盖过 ±1%。
        depth = get(f"{VISION}/api/v3/depth?symbol=BTCUSDT&limit=5000")
        bids, asks = depth.get("bids") or [], depth.get("asks") or []
        mid = _mid(bids[0][0] if bids else None, asks[0][0] if asks else None)
        imb05, tr05 = book_imbalance(bids, asks, 0.005)
        imb10, tr10 = book_imbalance(bids, asks, 0.01)
        return {
            "mid": _r(mid, 2),
            "imb_05": _r(imb05, 6),
            "imb_10": _r(imb10, 6),
            "truncated_05": bool(tr05),
            "truncated_10": bool(tr10),
            "bid_levels": len(bids),
            "ask_levels": len(asks),
        }

    def cvd():
        rows = get(f"{VISION}/api/v3/klines?symbol=BTCUSDT&interval=1m&limit=15")
        got = cvd_from_klines(rows)
        if not got:
            raise SnapError("没有 1 分钟 K 线")
        return got

    def deribit():
        end = int(now.timestamp() * 1000)
        start = end - 6 * 3600 * 1000
        vol = get(f"{DERIBIT}/get_volatility_index_data?currency=BTC&resolution=3600&start_timestamp={start}&end_timestamp={end}")
        data = ((vol.get("result") or {}).get("data")) or []
        if not data:
            raise SnapError("DVOL 为空")
        book = get(f"{DERIBIT}/get_book_summary_by_currency?currency=BTC&kind=option")
        surface = deribit_surface((book.get("result") or []), now)
        surface["dvol"] = _r(data[-1][4], 4)
        return surface

    take("book", spot_book)
    if isinstance(out.get("book"), dict) and out["book"].get("mid"):
        out["spot"] = out["book"]["mid"]
    take("cvd_15m", cvd)
    take("deribit", deribit)
    exchanges = {}

    def put_ex(key, fn):
        try:
            exchanges[key] = fn()
        except Exception as ex:
            msg = str(ex)[:180]
            exchanges[key] = {"error": msg}
            errors.append(f"{key}: {msg}")

    for key, name in (("okx", "okx"), ("bitget", "bitget"), ("gate", "gate"), ("hyperliquid", "hyperliquid"),
                      ("binance_perp", "binanceusdm"), ("bybit", "bybit")):
        # 币安合约在云主机上 451、Bybit 403，失败只记在这一家
        put_ex(key, lambda name=name: EX.perp_snapshot(name, ex_get))
    out["exchanges"] = exchanges
    try:
        # 币安价现取：前面几家交易所要十几秒，用开头的盘口中间价会和 Coinbase 差出这段时间的行情
        tick = get(f"{VISION}/api/v3/ticker/bookTicker?symbol=BTCUSDT")
        bn = _mid(tick.get("bidPrice"), tick.get("askPrice"))
        out["coinbase_premium_pct"] = _r(EX.coinbase_premium_pct(bn, ex_get), 6)
    except Exception as ex:
        msg = str(ex)[:180]
        out["coinbase_premium_pct"] = None
        out["coinbase_error"] = msg
        errors.append(f"coinbase: {msg}")
    out["errors"] = errors
    return out


def snapshot_line(snap):
    return json.dumps(snap, ensure_ascii=False, separators=(",", ":"))


# ---------------- 追加到分支 ----------------

class BranchMissing(Exception):
    pass


class FileMissing(Exception):
    pass


class GitHubBranch:
    """对一个分支做「读文件 / 追加 / 必要时建空分支」。"""

    def __init__(self, repo, token, branch, log=print):
        self.repo, self.token, self.branch, self.log = repo, token, branch, log

    def _call(self, method, path, body=None, ok=(200, 201)):
        req = urllib.request.Request(
            API + path, method=method,
            data=None if body is None else json.dumps(body).encode(),
            headers={"Authorization": f"Bearer {self.token}", "Accept": "application/vnd.github+json",
                     "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "btc-monitor",
                     "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                raw = r.read()
                return json.loads(raw.decode()) if raw else {}
        except urllib.error.HTTPError as ex:
            detail = ex.read().decode(errors="replace")[:300]
            if ex.code == 404 and "No commit found" in detail:
                raise BranchMissing(detail) from ex
            if ex.code == 404:
                raise FileMissing(detail) from ex
            raise SnapError(f"GitHub {ex.code} {path}: {detail}") from ex

    def branch_exists(self):
        try:
            self._call("GET", f"/repos/{self.repo}/git/ref/heads/{self.branch}")
            return True
        except BranchMissing:
            return False
        except FileMissing:
            return False

    def read_text(self, path):
        quoted = urllib.parse.quote(path)
        try:
            got = self._call("GET", f"/repos/{self.repo}/contents/{quoted}?ref={self.branch}")
        except FileMissing:
            raise
        except BranchMissing:
            raise
        if isinstance(got, list):
            raise SnapError(f"{path} 是目录")
        return base64.b64decode(got["content"]).decode("utf-8"), got["sha"]

    def _put(self, path, text, message, sha=None):
        body = {"message": message, "content": base64.b64encode(text.encode()).decode(), "branch": self.branch}
        if sha:
            body["sha"] = sha
        quoted = urllib.parse.quote(path)
        self._call("PUT", f"/repos/{self.repo}/contents/{quoted}", body, ok=(200, 201))

    def _create_orphan(self, files, message):
        tree = []
        for path, text in files.items():
            blob = self._call("POST", f"/repos/{self.repo}/git/blobs",
                              {"content": base64.b64encode(text.encode()).decode(), "encoding": "base64"})
            tree.append({"path": path, "mode": "100644", "type": "blob", "sha": blob["sha"]})
        t = self._call("POST", f"/repos/{self.repo}/git/trees", {"tree": tree})
        c = self._call("POST", f"/repos/{self.repo}/git/commits",
                       {"message": message, "tree": t["sha"], "parents": []})
        self._call("POST", f"/repos/{self.repo}/git/refs",
                   {"ref": f"refs/heads/{self.branch}", "sha": c["sha"]})

    def ensure_docs(self, files):
        """分支已经存在时，补上还没有的说明文件。不改已经有的内容。"""
        if not self.branch_exists():
            return
        for path, text in files.items():
            try:
                self.read_text(path)
            except FileMissing:
                body = text if text.endswith("\n") else text + "\n"
                self._put(path, body, "因子快照说明")

    def append_line(self, path, line, message, seed_files=None):
        """把一行加到文件末尾。分支或文件不存在就建。已有相同行则不动。返回是否新写入。"""
        line = line if line.endswith("\n") else line + "\n"
        if not self.branch_exists():
            files = dict(seed_files or {})
            files[path] = line
            self._create_orphan(files, message)
            return True
        try:
            text, sha = self.read_text(path)
        except FileMissing:
            self._put(path, line, message)
            return True
        if line in text or line.strip() in text:
            return False
        self._put(path, text + ("" if text.endswith("\n") or text == "" else "\n") + line, message, sha=sha)
        return True


def _day_of(snap):
    return snap["t"][:10]


class FactorRecorder:
    def __init__(self, out_dir, log=print, client=None):
        self.out, self.log = out_dir, log
        self.every = int(os.environ.get("FACTOR_EVERY", "900"))
        self.branch_name = os.environ.get("FACTOR_BRANCH", "factor-history")
        self.repo = os.environ.get("GITHUB_REPO", "")
        self.token = os.environ.get("GITHUB_TOKEN", "")
        self.client = client
        if self.client is None and self.repo and self.token:
            self.client = GitHubBranch(self.repo, self.token, self.branch_name, log)
        self.queue_path = os.path.join(out_dir, "factor_queue.jsonl")
        self.local_dir = os.path.join(out_dir, "factor-history")
        self.next_at = 0
        where = f"{self.repo} 的 {self.branch_name} 分支" if self.client else f"本机 {self.local_dir}（未配置推送）"
        log(f"因子快照：每 {self.every // 60} 分钟追加到 {where}")

    def step(self):
        if time.time() < self.next_at:
            return
        try:
            self.run_once()
            self.next_at = time.time() + self.every
        except Exception as ex:
            self.log("因子快照失败，5 分钟后再试:", ex)
            self.next_at = time.time() + 300

    def run_once(self, snap=None):
        os.makedirs(self.out, exist_ok=True)
        snap = snap if snap is not None else collect_snapshot()
        self._enqueue(snap)
        self.flush()
        n_err = len(snap.get("errors") or [])
        self.log(f"因子快照 {snap['t']} 已记录，{n_err} 个来源失败")
        return snap

    def _enqueue(self, snap):
        line = snapshot_line(snap)
        prev = ""
        if os.path.exists(self.queue_path):
            prev = open(self.queue_path, encoding="utf-8").read()
        if line in prev:
            return
        with open(self.queue_path, "a", encoding="utf-8") as f:
            f.write(line + "\n")

    def _pending(self):
        if not os.path.exists(self.queue_path):
            return []
        rows = []
        for line in open(self.queue_path, encoding="utf-8"):
            line = line.strip()
            if line:
                rows.append(json.loads(line))
        return rows

    def flush(self):
        rows = self._pending()
        if not rows:
            return
        if self.client is None:
            self._write_local(rows)
            self._clear_queue()
            return
        by_day = {}
        for snap in rows:
            by_day.setdefault(_day_of(snap), []).append(snap)
        seed = {
            "schema.json": json.dumps(SCHEMA, ensure_ascii=False, indent=1) + "\n",
            "README.md": "因子快照，只追加不覆盖。字段见 schema.json。面板不读这个分支。\n",
        }
        self.client.ensure_docs(seed)
        for day, snaps in sorted(by_day.items()):
            path = f"snapshots/{day}.jsonl"
            for snap in snaps:
                self.client.append_line(path, snapshot_line(snap), f"因子快照 {snap['t']}", seed_files=seed)
                seed = None  # 分支已经在了
        self._clear_queue()

    def _write_local(self, rows):
        os.makedirs(self.local_dir, exist_ok=True)
        schema = os.path.join(self.local_dir, "schema.json")
        if not os.path.exists(schema):
            with open(schema, "w", encoding="utf-8") as f:
                json.dump(SCHEMA, f, ensure_ascii=False, indent=1)
        by_day = {}
        for snap in rows:
            by_day.setdefault(_day_of(snap), []).append(snap)
        for day, snaps in by_day.items():
            path = os.path.join(self.local_dir, f"{day}.jsonl")
            existing = open(path, encoding="utf-8").read() if os.path.exists(path) else ""
            with open(path, "a", encoding="utf-8") as f:
                for snap in snaps:
                    line = snapshot_line(snap)
                    if line in existing:
                        continue
                    f.write(line + "\n")
                    existing += line + "\n"

    def _clear_queue(self):
        if os.path.exists(self.queue_path):
            os.remove(self.queue_path)


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="抓一次因子快照")
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--out", default=OUT)
    ap.add_argument("--no-push", action="store_true", help="只写本机，不推送到因子分支")
    args = ap.parse_args()
    rec = FactorRecorder(args.out, print, client=None if args.no_push else None)
    if args.no_push:
        rec.client = None
    snap = rec.run_once()
    print(json.dumps({"t": snap["t"], "errors": snap.get("errors"), "spot": snap.get("spot"),
                      "dvol": (snap.get("deribit") or {}).get("dvol") if isinstance(snap.get("deribit"), dict) else None,
                      "premium": snap.get("coinbase_premium_pct")}, ensure_ascii=False, indent=1))
