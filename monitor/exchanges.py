"""各交易所公开行情，统一走 ccxt。

ccxt 负责各家接口的差异、限频和重试；这里只把结果整理成监控和因子快照要的字段。
只读公开行情，不需要 API 密钥，也不下单。

没放进来的：
- 币安 K 线：监控要币安原始格式里的主动买入量（第 10 列），ccxt 的 fetch_ohlcv 只给 OHLCV，
  所以 K 线仍在 market_data.py 里直接读公开行情镜像。
- OKX 的持仓历史和强平单：ccxt 没收录这两个路径，用 fetch2 按原路径请求，仍走 ccxt 的限频和报错。
"""
import ccxt

TIMEOUT_MS = 15000

# 各家 BTC 永续和现货在 ccxt 里的统一代号
PERP = {
    "okx": "BTC/USDT:USDT",
    "bitget": "BTC/USDT:USDT",
    "gate": "BTC/USDT:USDT",
    "bybit": "BTC/USDT:USDT",
    "binanceusdm": "BTC/USDT:USDT",
    "hyperliquid": "BTC/USDC:USDC",
}
SPOT = {"okx": "BTC/USDT", "bitget": "BTC/USDT", "gate": "BTC/USDT", "bybit": "BTC/USDT"}
FUNDING_HOURS = {"hyperliquid": 1}  # 其余 8 小时

_clients = {}


def client(name):
    """同一进程里每家只建一个，市场列表只拉一次。"""
    if name not in _clients:
        opts = {"timeout": TIMEOUT_MS, "enableRateLimit": True}
        if name == "binance_vision":
            # 币安公开行情镜像：云服务器上主站会 451，镜像能通。只要现货市场，别去碰合约接口
            ex = ccxt.binance({**opts, "options": {"fetchMarkets": {"types": ["spot"]}}})
            ex.urls["api"]["public"] = "https://data-api.binance.vision/api/v3"
        else:
            ex = getattr(ccxt, name)(opts)
        _clients[name] = ex
    return _clients[name]


def _f(v):
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    return None if x != x else x


def mid(t):
    """ticker 的买一卖一中间价，没有就用最新价。"""
    b, a = _f(t.get("bid")), _f(t.get("ask"))
    if b and a and b > 0 and a > 0:
        return (b + a) / 2.0
    return _f(t.get("last"))


def basis_pct(perp, ref):
    if not perp or not ref:
        return None
    return (perp / ref - 1.0) * 100.0


def _r(v, n=6):
    x = _f(v)
    return None if x is None else round(x, n)


def perp_snapshot(name, get=client):
    """一家交易所的资金费率、永续价、现货价和基差。字段和 factor_snap 的 schema 一致。"""
    ex = get(name)
    sym = PERP[name]
    hours = FUNDING_HOURS.get(name, 8)
    if name == "hyperliquid":
        # Hyperliquid 没有 BTC 现货订单簿，基差用标记价相对预言机
        fr = ex.fetch_funding_rates([sym])[sym]
        mark, oracle = _f(fr.get("markPrice")), _f(fr.get("indexPrice"))
        return {"funding": _r(fr.get("fundingRate"), 8), "interval_hours": hours,
                "mark": _r(mark, 2), "oracle": _r(oracle, 2),
                "basis_pct": _r(basis_pct(mark, oracle), 6), "basis_kind": "mark_vs_oracle"}
    fr = ex.fetch_funding_rate(sym)
    mark, index = _f(fr.get("markPrice")), _f(fr.get("indexPrice"))
    if name == "binanceusdm":
        return {"funding": _r(fr.get("fundingRate"), 8), "interval_hours": hours,
                "mark": _r(mark, 2), "index": _r(index, 2),
                "basis_pct": _r(basis_pct(mark, index), 6), "basis_kind": "mark_vs_index"}
    pm = mid(ex.fetch_ticker(sym))
    try:
        sm = mid(ex.fetch_ticker(SPOT[name]))
    except Exception:
        sm = None
    out = {"funding": _r(fr.get("fundingRate"), 8), "interval_hours": hours,
           "perp": _r(pm, 2), "spot": _r(sm, 2), "basis_pct": _r(basis_pct(pm, sm), 6),
           "basis_kind": "perp_vs_spot"}
    if mark:
        out["mark"] = _r(mark, 2)
    if index:
        out["index"] = _r(index, 2)
    return out


def coinbase_premium_pct(binance_mid, get=client):
    """Coinbase BTC-USD / (币安现货 × Coinbase USDT-USD) − 1，单位 %。"""
    cb = get("coinbaseexchange")
    btc = mid(cb.fetch_ticker("BTC/USD"))
    usd = mid(cb.fetch_ticker("USDT/USD")) or 1.0
    if not btc or not binance_mid:
        raise RuntimeError("Coinbase 或币安现货价格缺失")
    return (btc / (binance_mid * usd) - 1.0) * 100.0


# ---------------- 监控用：持仓、资金费率、多空比 ----------------

def open_interest_btc(name, get=client):
    """当前持仓量（BTC）。OKX 用 oiCcy（币），币安合约本身就按币计。"""
    r = get(name).fetch_open_interest(PERP[name])
    if name == "okx":
        return float(r["info"]["oiCcy"])
    return float(r["openInterestAmount"])


def funding_pct(name, get=client):
    """最近一次资金费率（%）。"""
    return float(get(name).fetch_funding_rate(PERP[name])["fundingRate"]) * 100


def binance_long_short(n, get=client):
    """币安全部账户多空比，最近 n 个 5 分钟，从旧到新。"""
    rows = get("binanceusdm").fetch_long_short_ratio_history(PERP["binanceusdm"], "5m", None, n)
    return [float(x["longShortRatio"]) for x in rows][-n:]


def okx_rubik(path, params, get=client):
    """OKX 交易大数据接口（rubik），返回 data 数组，新的在前。"""
    return get("okx").fetch2(path, "public", "GET", params)["data"]


def okx_liquidations(inst_family, get=client):
    return get("okx").fetch2("public/liquidation-orders", "public", "GET",
                             {"instType": "SWAP", "instFamily": inst_family, "state": "filled", "limit": "100"})
