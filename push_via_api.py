#!/usr/bin/env python
"""在 github.com 主域不可达的环境下推送代码（走 GitHub REST API）。

## 为什么需要它

2026-11-04 实测本机出网情况：

| 目标 | 结果 |
|------|------|
| `https://github.com`（HTTPS 克隆/推送） | 连接被重置（curl 000），走本机代理也失败 |
| `github.com:22` / `ssh.github.com:443` | SSH banner 被中间设备改写成 `SSH-2.0-5ec5cb2f`（GitHub 官方应为 `SSH-2.0-babeld-*`），公钥认证一律 `Permission denied` |
| `https://api.github.com` | **200，可用** |

所以常规 `git push` 无论 HTTPS 还是 SSH 都推不上去，但 REST API 通。GitHub 的
Git Data API 足以完整重建提交历史：`blob → tree → commit`，最后移动分支引用。

作者、提交者、时间戳（含时区）全部按本地原始 commit 对象还原，因此重建出的
commit sha **与本地完全一致**（脚本逐条比对并在末尾报告）。这一点很重要 ——
sha 一致意味着本地 `master` 与远端分支不会分叉，后续 `git fetch` 不会产生冲突。

## 用法

    python push_via_api.py --dry          # 只看计划，不写远端
    python push_via_api.py                # 推 master -> eventdrive-cron
    python push_via_api.py --ref main --branch main

凭据从 git 凭据管理器读取（`git credential fill`，host=github.com），不落盘、
不打印。要求该 token 对仓库有 push 权限。

## 注意

- **只能快进**：远端分支 HEAD 必须是本地分支的祖先，否则拒绝执行（避免覆盖远端）。
- 删除文件按 `sha: null` 的 tree 条目处理；不做重命名检测（改名＝删+增）。
- GitHub 偶发 500/502，脚本对 5xx 与网络错误自动重试 4 次。
- 该脚本不更新本地 `origin/<branch>` 引用，推完可手动对齐：
  `git update-ref refs/remotes/origin/<branch> <sha>`。
"""

import argparse
import base64
import json
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

API = "https://api.github.com"


def git(*args, binary=False):
    p = subprocess.run(["git", *args], capture_output=True)
    if p.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} 失败: {p.stderr.decode('utf-8', 'replace')[:300]}")
    return p.stdout if binary else p.stdout.decode("utf-8")


def get_token() -> str:
    p = subprocess.run(
        ["git", "credential", "fill"],
        input="protocol=https\nhost=github.com\n\n",
        capture_output=True, text=True, timeout=30,
    )
    for line in p.stdout.splitlines():
        if line.startswith("password="):
            return line.split("=", 1)[1]
    raise RuntimeError(
        "凭据管理器里没有 github.com 的 token。\n"
        "先执行一次 `git push`（会弹窗登录）或在 Git 凭据管理器中新增 github.com 条目。"
    )


class Github:
    def __init__(self, owner: str, repo: str):
        self.owner = owner
        self.repo = repo
        self.prefix = f"/repos/{owner}/{repo}"
        self.token = get_token()

    def _call(self, method, path, body=None, retries=4):
        data = json.dumps(body).encode() if body is not None else None
        last = ""
        for attempt in range(retries):
            req = urllib.request.Request(API + path, data=data, method=method)
            req.add_header("Authorization", "Bearer " + self.token)
            req.add_header("Accept", "application/vnd.github+json")
            req.add_header("User-Agent", "eventdrive-push")
            if data:
                req.add_header("Content-Type", "application/json")
            try:
                with urllib.request.urlopen(req, timeout=90) as r:
                    return json.load(r)
            except urllib.error.HTTPError as e:
                detail = e.read().decode("utf-8", "replace")[:300]
                last = f"{method} {path} -> HTTP {e.code}: {detail}"
                if e.code >= 500 and attempt < retries - 1:
                    wait = 2 + attempt * 2
                    print(f"      · HTTP {e.code}，{wait}s 后重试（{attempt + 1}/{retries - 1}）")
                    time.sleep(wait)
                    continue
                raise RuntimeError(last) from None
            except Exception as e:  # noqa: BLE001 —— 连接重置/超时同样值得重试
                last = f"{method} {path} -> {type(e).__name__}: {str(e)[:200]}"
                if attempt < retries - 1:
                    wait = 2 + attempt * 2
                    print(f"      · {type(e).__name__}，{wait}s 后重试（{attempt + 1}/{retries - 1}）")
                    time.sleep(wait)
                    continue
                raise RuntimeError(last) from None
        raise RuntimeError(last or "unknown")

    def get(self, path):
        return self._call("GET", path)

    def post(self, path, body):
        return self._call("POST", path, body)

    def patch(self, path, body):
        return self._call("PATCH", path, body)


def parse_ident(s: str) -> dict:
    """`Name <email> 1762000000 +0800` -> {name, email, date(ISO8601 带时区)}"""
    lt = s.rindex("<")
    gt = s.rindex(">")
    name = s[:lt].strip()
    email = s[lt + 1:gt]
    rest = s[gt + 1:].split()
    ts = int(rest[0])
    tz = rest[1] if len(rest) > 1 else "+0000"
    sign = -1 if tz.startswith("-") else 1
    off = sign * (int(tz[1:3]) * 3600 + int(tz[3:5]) * 60)
    dt = datetime.fromtimestamp(ts, tz=timezone(timedelta(seconds=off)))
    return {"name": name, "email": email, "date": dt.isoformat()}


def read_commit(sha: str) -> dict:
    raw = git("cat-file", "commit", sha)
    header, _, message = raw.partition("\n\n")
    info = {"tree": None, "parents": [], "author": None, "committer": None, "message": message}
    for line in header.splitlines():
        if line.startswith("tree "):
            info["tree"] = line[5:].strip()
        elif line.startswith("parent "):
            info["parents"].append(line[7:].strip())
        elif line.startswith("author "):
            info["author"] = parse_ident(line[7:])
        elif line.startswith("committer "):
            info["committer"] = parse_ident(line[10:])
    return info


def read_changes(parent: str, commit: str):
    """返回 [(status, mode, new_sha, path)]，status ∈ {A, M, D, T}。"""
    raw = git("diff-tree", "-r", "-z", "--no-renames", "--raw", parent, commit, binary=True)
    parts = raw.split(b"\x00")
    out = []
    i = 0
    while i < len(parts):
        head = parts[i].decode("utf-8", "replace").strip()
        if not head.startswith(":"):
            i += 1
            continue
        old_mode, new_mode, old_sha, new_sha, status = head[1:].split()[:5]
        path = parts[i + 1].decode("utf-8", "replace") if i + 1 < len(parts) else ""
        out.append((status[0], new_mode if status[0] != "D" else old_mode, new_sha, path))
        i += 2
    return out


def main():
    ap = argparse.ArgumentParser(description="通过 GitHub REST API 推送提交（github.com 不可达时使用）")
    ap.add_argument("--owner", default="zhoulzhou")
    ap.add_argument("--repo", default="EventDrive")
    ap.add_argument("--ref", default="master", help="要推送的本地分支（默认 master）")
    ap.add_argument("--branch", default="eventdrive-cron", help="目标远端分支（默认 eventdrive-cron）")
    ap.add_argument("--dry", action="store_true", help="只打印计划，不写远端")
    args = ap.parse_args()

    gh = Github(args.owner, args.repo)

    try:
        remote = gh.get(f"{gh.prefix}/branches/{args.branch}")
        base = remote["commit"]["sha"]
    except RuntimeError as e:
        if "HTTP 404" not in str(e):
            raise
        remote, base = None, None
        print(f"远端分支 {args.branch} 不存在，将新建。")

    head = git("rev-parse", args.ref).strip()
    if base is None:
        commits = git("rev-list", "--reverse", args.ref).split()
    else:
        is_ancestor = subprocess.run(
            ["git", "merge-base", "--is-ancestor", base, args.ref], capture_output=True
        ).returncode == 0
        if not is_ancestor:
            raise SystemExit(
                f"远端 {args.branch}（{base[:10]}）不是本地 {args.ref}（{head[:10]}）的祖先，\n"
                "说明两边已分叉。先 git fetch / 合并，别用本脚本硬推。"
            )
        commits = git("rev-list", "--reverse", f"{base}..{args.ref}").split()

    print(f"远端 {args.branch} = {base[:10] if base else '(空)'}")
    print(f"本地 {args.ref} = {head[:10]}，待推 {len(commits)} 个提交")
    if not commits:
        print("没有要推的提交，已是最新。")
        return

    blob_cache = {}
    parent_for_github = base
    # base_tree 必须用**远端已存在**的 tree：第一轮取远端 HEAD 的 tree，
    # 之后用上一步 GitHub 返回的 tree（本地 sha 若与远端有偏差，链条会断）
    tree_for_github = remote["commit"]["commit"]["tree"]["sha"] if remote else None
    created = []

    for idx, sha in enumerate(commits, 1):
        c = read_commit(sha)
        parent_local = c["parents"][0] if c["parents"] else None
        changes = read_changes(parent_local, sha) if parent_local else []
        subject = c["message"].splitlines()[0][:52] if c["message"] else ""
        print(f"\n[{idx}/{len(commits)}] {sha[:10]} {subject}")
        print(f"    变更 {len(changes)} 个文件")

        if args.dry:
            for st, mode, new_sha, path in changes:
                size = git("cat-file", "-s", new_sha).strip() if st != "D" else "-"
                print(f"      {st} {mode} {size:>7} {path}")
            tree_for_github = c["tree"]
            created.append((sha, "(dry-run)"))
            continue

        entries = []
        for st, mode, new_sha, path in changes:
            if st == "D":
                entries.append({"path": path, "mode": mode, "type": "blob", "sha": None})
                continue
            if new_sha not in blob_cache:
                content = git("cat-file", "blob", new_sha, binary=True)
                got = gh.post(f"{gh.prefix}/git/blobs", {
                    "content": base64.b64encode(content).decode(),
                    "encoding": "base64",
                })
                blob_cache[new_sha] = got["sha"]
            entries.append({"path": path, "mode": mode, "type": "blob",
                            "sha": blob_cache[new_sha]})

        tree_body = {"tree": entries}
        if tree_for_github:
            tree_body["base_tree"] = tree_for_github
        tree = gh.post(f"{gh.prefix}/git/trees", tree_body)

        commit_body = {
            "message": c["message"],
            "tree": tree["sha"],
            "parents": [parent_for_github] if parent_for_github else [],
            "author": c["author"],
            "committer": c["committer"],
        }
        commit_obj = gh.post(f"{gh.prefix}/git/commits", commit_body)
        flag = "sha 一致" if commit_obj["sha"] == sha else f"⚠ sha 不同（远端 {commit_obj['sha'][:10]}）"
        print(f"    -> {commit_obj['sha'][:10]} {flag}")

        parent_for_github = commit_obj["sha"]
        tree_for_github = tree["sha"]
        created.append((sha, commit_obj["sha"]))

    if args.dry:
        print("\n[dry-run] 未写入远端。")
        return

    if base is None:
        gh.post(f"{gh.prefix}/git/refs", {"ref": f"refs/heads/{args.branch}", "sha": parent_for_github})
    else:
        gh.patch(f"{gh.prefix}/git/refs/heads/{args.branch}",
                 {"sha": parent_for_github, "force": False})

    print(f"\n分支 {args.branch} 已更新为 {parent_for_github[:10]}")
    print(f"对齐本地引用：git update-ref refs/remotes/origin/{args.branch} {parent_for_github}")

    mismatch = [(a, b) for a, b in created if a != b]
    if mismatch:
        print(f"⚠ 有 {len(mismatch)} 个提交的 sha 与本地不同（内容一致，历史对象不同）：")
        for a, b in mismatch:
            print(f"   local {a[:10]} -> remote {b[:10]}")
    else:
        print(f"全部 {len(created)} 个提交的 sha 与本地完全一致。")


if __name__ == "__main__":
    sys.exit(main())
