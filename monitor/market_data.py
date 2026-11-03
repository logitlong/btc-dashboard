"""BTC 行情数据来源。

默认（本机）：币安 USDT 永续合约（fapi.binance.com）。
GitHub Actions 的服务器在美国，币安合约和主站接口会返回 451（地区限制），所以云上设
MARKET_SOURCE=cloud：
- K 线：币安公开行情镜像 data-api.binance.vision 的 BTCUSDT 现货（格式和合约 K 线一样，也有主动买入量）。
  现货和永续价格通常只差 0.01%–0.05%，方向模型在现货数据上的样本外准确率和永续相当（2017–19 现货检验 53.3%）。
- 持仓量、资金费率、多空比：OKX 的 BTC-USDT 永续（只看变化幅度，用来做盘面异常提醒）。
面板在浏览器里直接连币安，不受影响。

K 线直接读币安原始格式（要主动买入量那一列）；持仓量、资金费率、多空比、OKX 统计数据走 ccxt（exchanges.py）。
"""
import json
import os
import urllib.request

import exchanges as EX

SOURCE = os.environ.get("MARKET_SOURCE", "binance")
FAPI = "https://fapi.binance.com"
VISION = "https://data-api.binance.vision"


def _get(url, timeout=20):
    with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "btc-monitor/1.0"}), timeout=timeout) as r:
        return json.load(r)


def klines(interval, limit=1500, start_ms=None):
    """[[开盘时间, 开, 高, 低, 收, 量, 收盘时间, 成交额, 笔数, 主动买入量, ...], ...]，和币安格式一致。"""
    if SOURCE == "binance":
        url = f"{FAPI}/fapi/v1/klines?symbol=BTCUSDT&interval={interval}&limit={min(limit, 1500)}"
        return _get(url + (f"&startTime={int(start_ms)}" if start_ms is not None else ""))
    base = f"{VISION}/api/v3/klines?symbol=BTCUSDT&interval={interval}"
    if start_ms is not None:
        return _get(f"{base}&limit={min(limit, 1000)}&startTime={int(start_ms)}")
    first = _get(f"{base}&limit={min(limit, 1000)}")
    if limit > 1000 and first:                    # 现货接口一次最多 1000 根：往前再取一次拼起来
        first = _get(f"{base}&limit={limit - len(first)}&endTime={int(first[0][0] - 1)}") + first
    return first


def _perp():
    return "binanceusdm" if SOURCE == "binance" else "okx"


def open_interest():
    """当前持仓量（BTC）。"""
    return EX.open_interest_btc(_perp())


def funding_pct():
    """最近一次资金费率（%）。"""
    return EX.funding_pct(_perp())


def long_short(n=4):
    """最近 n 个 5 分钟的多空账户比，从旧到新。"""
    if SOURCE == "binance":
        return EX.binance_long_short(n)
    d = EX.okx_rubik("rubik/stat/contracts/long-short-account-ratio", {"ccy": "BTC", "period": "5m"})
    return [float(x[1]) for x in d[:n]][::-1]


def spot_klines(interval, limit=1000, start_ms=None):
    """币安公开现货 K 线。先走公开行情镜像，不行再走主站。格式和合约 K 线一样。"""
    base_paths = (f"{VISION}/api/v3/klines?symbol=BTCUSDT&interval={interval}",
                  f"https://api.binance.com/api/v3/klines?symbol=BTCUSDT&interval={interval}")
    last = None
    for base in base_paths:
        try:
            url = f"{base}&limit={min(limit, 1000)}" + (f"&startTime={int(start_ms)}" if start_ms is not None else "")
            return _get(url)
        except Exception as ex:
            last = ex
    raise last


def okx_coin_oi_change():
    """OKX 币本位 BTC-USD-SWAP 持仓（币）最近两根 1 小时的变化，百分比。"""
    d = EX.okx_rubik("rubik/stat/contracts/open-interest-history", {"instId": "BTC-USD-SWAP", "period": "1H"})
    now, old = float(d[0][2]), float(d[1][2])
    return dict(now=now, old=old, chg=(now / old - 1) * 100 if old else 0.0)


def okx_ls_1h():
    """OKX 多空账户比，最新一根 1 小时相对上一根的差值（不是百分比）。"""
    d = EX.okx_rubik("rubik/stat/contracts/long-short-account-ratio", {"ccy": "BTC", "period": "1H"})
    now, old = float(d[0][1]), float(d[1][1])
    return dict(now=now, old=old, chg=now - old)


def okx_taker_1h():
    """OKX 近一根 1 小时主动卖、主动买（美元）。"""
    d = EX.okx_rubik("rubik/stat/taker-volume", {"ccy": "BTC", "instType": "CONTRACTS", "period": "1H"})[0]
    return dict(sell=float(d[1]), buy=float(d[2]))


def spot_change_1h():
    """现货最近一根 1 小时（含未收盘）相对上一根收盘的涨跌，百分比。"""
    k = spot_klines("1h", 2)
    prev, cur = float(k[-2][4]), float(k[-1][4])
    return (cur / prev - 1) * 100 if prev else 0.0


def okx_liq_1h():
    """OKX 公开强平：BTC 本位和 U 本位近 1 小时名义美元。多单被强平 / 空单被强平分开。"""
    import time
    now, long_usd, short_usd = time.time() * 1000, 0.0, 0.0
    for fam, kind in (("BTC-USDT", "linear"), ("BTC-USD", "inverse")):
        d = EX.okx_liquidations(fam)
        for row in d.get("data") or []:
            for det in row.get("details") or []:
                if now - int(det["ts"]) > 3600e3:
                    continue
                sz, px = float(det["sz"]), float(det.get("bkPx") or 0)
                usd = sz * 0.01 * px if kind == "linear" else sz * 100
                if det.get("posSide") == "long":
                    long_usd += usd
                else:
                    short_usd += usd
    return dict(long=long_usd, short=short_usd, source="OKX")
