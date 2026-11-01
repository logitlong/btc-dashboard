"""把面板需要的数据推送到 GitHub 仓库的 data 分支（面板从 raw.githubusercontent.com 读取）。

只用标准库，通过 GitHub API 提交，不需要在服务器上装 git。
每次推送都生成一个没有父提交的新提交并强制覆盖 data 分支，所以分支永远只有一个提交，仓库不会越变越大。

环境变量：
  GITHUB_REPO    例如 logitlong/btc-dashboard
  GITHUB_TOKEN   细粒度令牌，只授权这个仓库的 Contents: Read and write
  PUBLISH_EVERY  推送间隔秒数，默认 120
"""
import base64
import json
import os
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
API = "https://api.github.com"


class Publisher:
    def __init__(self, out_dir, log=print):
        self.out, self.log = out_dir, log
        self.repo = os.environ.get("GITHUB_REPO", "")
        self.token = os.environ.get("GITHUB_TOKEN", "")
        self.every = int(os.environ.get("PUBLISH_EVERY", "120"))
        self.branch = os.environ.get("PUBLISH_BRANCH", "data")
        self.enabled = bool(self.repo and self.token)
        self.next_at = 0
        log(f"数据发布：{'每 ' + str(self.every) + ' 秒推送到 ' + self.repo + ' 的 ' + self.branch + ' 分支' if self.enabled else '未配置 GITHUB_REPO / GITHUB_TOKEN，不推送'}")

    def _call(self, method, path, body=None):
        req = urllib.request.Request(API + path, method=method, data=json.dumps(body).encode() if body is not None else None,
                                     headers={"Authorization": f"Bearer {self.token}", "Accept": "application/vnd.github+json",
                                              "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "btc-monitor"})
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.load(r)

    def files(self):
        out = {}
        for name in ("status.json", "paper.json", "model_calls.json", "liq.json"):
            p = os.path.join(self.out, name)
            if os.path.exists(p):
                out[name] = open(p, "rb").read()
        out["rules_spec.json"] = open(os.path.join(ROOT, "rules_spec.json"), "rb").read()
        return out

    def push(self):
        repo = self.repo
        tree = []
        for name, content in self.files().items():
            blob = self._call("POST", f"/repos/{repo}/git/blobs", {"content": base64.b64encode(content).decode(), "encoding": "base64"})
            tree.append({"path": name, "mode": "100644", "type": "blob", "sha": blob["sha"]})
        t = self._call("POST", f"/repos/{repo}/git/trees", {"tree": tree})
        c = self._call("POST", f"/repos/{repo}/git/commits", {"message": "数据更新", "tree": t["sha"], "parents": []})
        try:
            self._call("PATCH", f"/repos/{repo}/git/refs/heads/{self.branch}", {"sha": c["sha"], "force": True})
        except urllib.error.HTTPError as ex:
            if ex.code != 422:
                raise
            self._call("POST", f"/repos/{repo}/git/refs", {"ref": f"refs/heads/{self.branch}", "sha": c["sha"]})

    def step(self):
        if not self.enabled or time.time() < self.next_at:
            return
        self.next_at = time.time() + self.every
        try:
            self.push()
        except urllib.error.HTTPError as ex:
            if ex.code in (401, 403, 404):
                self.log(f"数据发布失败（{ex.code}）：检查 GITHUB_TOKEN 是否有这个仓库的 Contents 写权限，GITHUB_REPO 是否正确")
                self.next_at = time.time() + 900
            else:
                self.log(f"数据发布失败（{ex.code}），稍后重试")
        except Exception as ex:
            self.log("数据发布失败，稍后重试:", ex)


if __name__ == "__main__":
    p = Publisher(os.path.join(HERE, "out"))
    if p.enabled:
        p.push(); print("已推送")
