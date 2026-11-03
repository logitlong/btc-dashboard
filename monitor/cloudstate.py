"""让后台程序可以分段运行（GitHub Actions 每次最多跑一小时左右）：保存 / 恢复内存里的状态，
把提醒写进私有仓库给 Muse 读取。

- monitor_state.json：新闻抓取时间表和去重簇、盘面冷却和持仓量历史、最近警报。和 events.db、
  paper_state.json 一起由 StatePublisher 定期推到本仓库的 monitor-state 分支（公开内容，不含人工喊单），
  下一段运行开始前取回。
- 提醒（红 / 黄 / 持仓相关）写进私有仓库 btc-records 的 alerts.json（最近 300 条），Muse 定时读取新的条目推送给用户。
  需要 RECORDS_REPO 和 RECORDS_TOKEN（没有就退回 GITHUB_TOKEN）。

只用标准库。
"""
import base64
import hashlib
import json
import os
import time
import urllib.error
import urllib.request
from datetime import datetime
from zoneinfo import ZoneInfo

TZ = ZoneInfo(os.environ.get("DISPLAY_TZ", "Asia/Tokyo"))      # 提醒时间按东京时间写，Muse 推送时直接可读

from publish import Publisher

STATE_FILES = ("events.db", "paper_state.json", "monitor_state.json", "market_log.csv", "alerts.jsonl", "zhang_alert_state.json",
               "vol_model.json", "factor_queue.jsonl")


def _dt(s):
    return datetime.fromisoformat(s)


def save_state(out, news, market, notifier):
    st = dict(saved=time.time(),
              news=dict(next_at=news.next_at, fail=news.fail,
                        clusters=[[c[0].isoformat(), sorted(c[1]), c[2], sorted(c[3]), c[4]] for c in news.clusters]),
              market=dict(cool=market.cool, oi_hist=market.oi_hist, pos_at=getattr(market, "pos_at", 0),
                          slope_state=getattr(market, "slope_state", None), slope_bar=getattr(market, "slope_bar", None),
                          minute_seen=getattr(market, "minute_seen", {})),
              notifier=dict(recent=notifier.recent, queue=notifier.queue, last_flush=notifier.last_flush))
    tmp = os.path.join(out, "monitor_state.json.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(st, f, ensure_ascii=False)
    os.replace(tmp, os.path.join(out, "monitor_state.json"))


def load_state(out, news, market, notifier, log):
    p = os.path.join(out, "monitor_state.json")
    if not os.path.exists(p):
        return False
    try:
        st = json.load(open(p, encoding="utf-8"))
        news.next_at.update({k: v for k, v in st["news"]["next_at"].items() if k in news.next_at})
        news.fail.update({k: v for k, v in st["news"]["fail"].items() if k in news.fail})
        news.clusters = [[_dt(c[0]), set(c[1]), c[2], set(c[3]), c[4]] for c in st["news"]["clusters"]]
        news.first_pass = {k: False for k in news.first_pass}     # 之前跑过：新抓到的就是新消息，照常推送
        market.cool = st["market"]["cool"]
        market.oi_hist = [tuple(x) for x in st["market"]["oi_hist"]]
        market.pos_at = st["market"].get("pos_at", 0)
        market.slope_state = st["market"].get("slope_state")
        market.slope_bar = st["market"].get("slope_bar")
        market.minute_seen = st["market"].get("minute_seen") or {}
        notifier.recent = st["notifier"]["recent"]
        notifier.queue = st["notifier"]["queue"]
        notifier.last_flush = st["notifier"]["last_flush"]
        log(f"接着上次的状态运行（保存于 {round((time.time() - st['saved']) / 60)} 分钟前）")
        return True
    except Exception as ex:
        log("上次的状态读取失败，从头开始:", ex)
        return False


class StatePublisher(Publisher):
    """把状态文件推到本仓库的 monitor-state 分支（单个提交、强制覆盖），默认每 10 分钟一次，退出前再推一次。"""

    def __init__(self, out_dir, log=print):
        super().__init__(out_dir, lambda *a: None)
        self.log = log
        self.branch = os.environ.get("STATE_BRANCH", "monitor-state")
        self.every = int(os.environ.get("STATE_EVERY", "600"))
        self.enabled = self.enabled and os.environ.get("SAVE_STATE") == "1"
        self.next_at = time.time() + self.every
        if self.enabled:
            log(f"状态保存：每 {self.every // 60} 分钟推到 {self.repo} 的 {self.branch} 分支")

    def files(self):
        return {n: open(os.path.join(self.out, n), "rb").read() for n in STATE_FILES if os.path.exists(os.path.join(self.out, n))}


class Outbox:
    """提醒发件箱：私有仓库 btc-records 的 alerts.json，Muse 读它推送。没有配置私有仓库时只记在本地。"""

    def __init__(self, out_dir, log=print):
        self.log = log
        self.repo = os.environ.get("RECORDS_REPO", "")
        # 本机可以退回 gh 登录的令牌；在 GitHub Actions 里自带的 GITHUB_TOKEN 读不了私有仓库，必须单独配 RECORDS_TOKEN
        self.token = os.environ.get("RECORDS_TOKEN") or ("" if os.environ.get("GITHUB_ACTIONS") == "true" else os.environ.get("GITHUB_TOKEN", ""))
        self.enabled = bool(self.repo and self.token)
        self.items, self.sha, self.dirty, self.loaded = [], None, False, False
        self.path = os.path.join(out_dir, "outbox.json")

    def _req(self, method, body=None):
        req = urllib.request.Request(f"https://api.github.com/repos/{self.repo}/contents/alerts.json", method=method,
                                     data=json.dumps(body).encode() if body is not None else None,
                                     headers={"Authorization": f"Bearer {self.token}", "Accept": "application/vnd.github+json",
                                              "User-Agent": "btc-monitor/1.0", "Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.load(r)

    def _load(self):
        if self.loaded or not self.enabled:
            return
        try:
            j = self._req("GET")
            self.sha = j["sha"]
            remote = json.loads(base64.b64decode(j["content"]).decode("utf-8")).get("alerts", [])
            ids = {a["id"] for a in remote}
            self.items = remote + [a for a in self.items if a["id"] not in ids]
        except urllib.error.HTTPError as ex:
            if ex.code != 404:
                raise
        self.loaded = True

    def add(self, level, kind, title, body, email=False, subject=None):
        t = time.time()
        a = dict(id=f"{int(t * 1000)}-{hashlib.md5((title + body).encode()).hexdigest()[:6]}", time=datetime.fromtimestamp(t, TZ).isoformat(timespec="seconds"),
                 level=level, kind=kind, title=title, body=body, email=bool(email), subject=subject or title)
        self.items.append(a)
        self.items = self.items[-300:]
        self.dirty = True

    def flush(self):
        if not self.dirty:
            return
        if not self.enabled:
            json.dump({"alerts": self.items}, open(self.path, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
            self.dirty = False
            return
        for _ in range(3):
            try:
                self._load()
                body = {"message": "新提醒", "content": base64.b64encode(json.dumps({"updated": datetime.now(TZ).isoformat(timespec="seconds"), "alerts": self.items},
                                                                                  ensure_ascii=False, indent=1).encode()).decode()}
                if self.sha:
                    body["sha"] = self.sha
                self.sha = self._req("PUT", body)["content"]["sha"]
                self.dirty = False
                return
            except urllib.error.HTTPError as ex:
                if ex.code in (409, 422):        # 别处刚改过：重新读再写
                    self.loaded = False
                    continue
                self.log(f"提醒写入私有仓库失败（{ex.code}），下一轮再试")
                return
            except Exception as ex:
                self.log("提醒写入私有仓库失败，下一轮再试:", ex)
                return
