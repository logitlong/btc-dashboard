"""全网 BTC 永续爆仓：从 Coinalyze 取各交易所爆仓历史，汇总成 out/liq.json 给面板读。

每 5 分钟取一次最近 25 小时的 5 分钟数据，金额换算成美元；面板再按所选周期合并。Coinalyze 每个合约算一次调用，
限额每分钟 40 次，所以合约数封顶 BTC_MAX_SYMBOLS。
"全网"只是 Coinalyze 接入的交易所相加；币安公开的爆仓流本身是抽样的，数字可能偏低。
密钥放环境变量 COINALYZE_API_KEY（GitHub Secret），不写进面板和输出文件。只用标准库。
"""
import json
import os
import time
import urllib.parse
import urllib.request

API = "https://api.coinalyze.net/v1"
EVERY = 300
WINDOW = 25 * 3600
BTC_MAX_SYMBOLS = 35
QUOTES = {"USDT": 0, "USD": 1, "USDC": 2}


class Liquidations:
    def __init__(self, out_dir, log=print):
        self.path, self.log = os.path.join(out_dir, "liq.json"), log
        self.key = os.environ.get("COINALYZE_API_KEY", "")
        self.enabled = bool(self.key)
        self.next_at = 0
        self.symbols, self.names = [], {}
        log(f"全网爆仓：{'每 5 分钟从 Coinalyze 更新' if self.enabled else '未配置 COINALYZE_API_KEY，跳过'}")

    def _get(self, path, **q):
        url = f"{API}/{path}" + ("?" + urllib.parse.urlencode(q, safe=",") if q else "")
        req = urllib.request.Request(url, headers={"api_key": self.key, "User-Agent": "btc-monitor/1.0"})
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.load(r)

    def load_markets(self):
        self.names = {x["code"]: x["name"] for x in self._get("exchanges")}
        rows = [x for x in self._get("future-markets") if x.get("is_perpetual") and x.get("base_asset") == "BTC" and x.get("quote_asset") in QUOTES]
        rows.sort(key=lambda x: (QUOTES[x["quote_asset"]], x["symbol"]))
        self.symbols = [x["symbol"] for x in rows[:BTC_MAX_SYMBOLS]]
        self.log(f"全网爆仓：{len(rows)} 个 BTC 永续，取 {len(self.symbols)} 个")

    def fetch(self):
        if not self.symbols:
            self.load_markets()
            time.sleep(2)
        now, rows = int(time.time()), []
        for i in range(0, len(self.symbols), 20):
            part = ",".join(self.symbols[i:i + 20])
            rows += self._get("liquidation-history", symbols=part, interval="5min", convert_to_usd="true", **{"from": now - WINDOW, "to": now})
        return self.summarize(rows, now)

    def summarize(self, rows, now):
        bars, ex, cut = {}, {}, now - 86400
        for r in rows:
            name = self.names.get(r["symbol"].rsplit(".", 1)[-1], r["symbol"].rsplit(".", 1)[-1])
            e = ex.setdefault(name, {"name": name, "l": 0.0, "s": 0.0})
            for h in r.get("history") or []:
                b = bars.setdefault(h["t"], [0.0, 0.0])
                b[0] += h.get("l") or 0
                b[1] += h.get("s") or 0
                if h["t"] >= cut:
                    e["l"] += h.get("l") or 0
                    e["s"] += h.get("s") or 0
        exchanges = sorted((e for e in ex.values() if e["l"] + e["s"] > 0), key=lambda e: -(e["l"] + e["s"]))
        return {"updated_ms": int(time.time() * 1000), "symbols": len(self.symbols),
                "bars": [[t * 1000, round(b[0]), round(b[1])] for t, b in sorted(bars.items())],
                "exchanges": [{"name": e["name"], "l": round(e["l"]), "s": round(e["s"])} for e in exchanges]}

    def step(self):
        if not self.enabled or time.time() < self.next_at:
            return
        self.next_at = time.time() + EVERY
        try:
            data = self.fetch()
            with open(self.path + ".tmp", "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False)
            os.replace(self.path + ".tmp", self.path)
        except Exception as ex:
            self.log("全网爆仓更新失败，稍后重试:", type(ex).__name__, getattr(ex, "code", ""))


if __name__ == "__main__":
    liq = Liquidations(os.path.join(os.path.dirname(os.path.abspath(__file__)), "out"))
    if liq.enabled:
        d = liq.fetch()
        print(len(d["bars"]), "根", [(e["name"], e["l"], e["s"]) for e in d["exchanges"]])
