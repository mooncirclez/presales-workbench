#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""从 CHANGELOG.md 里取出某个版本那一段 —— 发版流程用。

为什么单独成文件:GitHub Actions 与本地 bin/release.sh 都要用同一份提取逻辑,
写在 workflow 的 yaml 里既没法本地验证,改起来也容易两边跑偏。

用法:
  python3 bin/changelog.py 0.2.0            # 打印该版本正文(发布说明用)
  python3 bin/changelog.py 0.2.0 --title    # 打印该版本标题行
  python3 bin/changelog.py --latest         # 打印最新版本号
  python3 bin/changelog.py --check 0.3.0    # 确认该版本已写好日志(退出码 0/1)
"""
import argparse
import re
import sys
from pathlib import Path

CHANGELOG = Path(__file__).resolve().parent.parent / "CHANGELOG.md"
# 形如:## [0.2.0] — 2026-07-28
HEAD_RE = re.compile(r"^## \[(?P<ver>[^\]]+)\]\s*(?:[—–-]\s*(?P<date>\S+))?\s*$", re.M)


def sections(text):
    """切成 [(版本号, 日期, 正文), …],顺序与文件一致(新版在前)。"""
    hits = list(HEAD_RE.finditer(text))
    out = []
    for i, m in enumerate(hits):
        start = m.end()
        end = hits[i + 1].start() if i + 1 < len(hits) else len(text)
        body = text[start:end]
        # 去掉版本之间的分隔线与文末的链接定义,它们不属于发布说明
        body = re.sub(r"^---\s*$", "", body, flags=re.M)
        body = re.sub(r"^\[[^\]]+\]:\s*http\S+\s*$", "", body, flags=re.M)
        out.append((m.group("ver"), m.group("date") or "", body.strip()))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("version", nargs="?", help="版本号,如 0.2.0(可带 v 前缀)")
    ap.add_argument("--title", action="store_true", help="只打印标题行")
    ap.add_argument("--latest", action="store_true", help="打印最新版本号")
    ap.add_argument("--check", metavar="VER", help="确认该版本已写好日志")
    args = ap.parse_args()

    if not CHANGELOG.exists():
        print("找不到 CHANGELOG.md", file=sys.stderr)
        return 1
    secs = sections(CHANGELOG.read_text(encoding="utf-8"))
    if not secs:
        print("CHANGELOG.md 里没有解析到任何版本段落", file=sys.stderr)
        return 1

    if args.latest:
        print(secs[0][0])
        return 0

    want = (args.check or args.version or "").lstrip("vV")
    if not want:
        print("需要版本号,或用 --latest", file=sys.stderr)
        return 1

    hit = next((s for s in secs if s[0] == want), None)
    if hit is None:
        print(f"CHANGELOG.md 里没有 {want} 这一段(现有:{', '.join(s[0] for s in secs)})",
              file=sys.stderr)
        return 1
    if args.check:
        if not hit[2]:
            print(f"{want} 段落是空的,先把更新内容写进去", file=sys.stderr)
            return 1
        return 0

    ver, date, body = hit
    if args.title:
        # 取正文第一句当副标题:一句话说清这版干了什么(整行往往太长)
        lead = next((ln.strip() for ln in body.splitlines() if ln.strip()), "")
        lead = re.sub(r"[*`]", "", lead)
        lead = re.split(r"[。;;]", lead)[0].strip()[:60]
        print(f"v{ver}" + (f" — {lead}" if lead else ""))
    else:
        print(body)
    return 0


if __name__ == "__main__":
    sys.exit(main())
