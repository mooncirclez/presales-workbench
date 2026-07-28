#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""售前工作台 · 本地服务(纯标准库,Python 3.9+)

启动:
  python3 bin/server.py            # http://127.0.0.1:8917
  python3 bin/server.py --port 9000

提供:
  - Web 界面(bin/app.html)
  - REST API:客户/项目/纪要/文件读写/模板实例化/全文搜索/AI 任务(调用本机 claude CLI)
安全:
  - 默认只绑定 127.0.0.1
  - 所有路径限制在工作台根目录内;写操作仅限数据区目录与文本类文件
"""
import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import uuid
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs, unquote

BIN = Path(__file__).resolve().parent
ROOT = BIN.parent
sys.path.insert(0, str(BIN))
import workbench as wb  # noqa: E402  复用 sync 逻辑(mcp 合并、skills 复制、状态收集)

DATA_DIRS = {"customers", "projects", "knowledge", "inbox", "archive", "scenarios",
             "calendar", "collab"}
EDIT_SUFFIX = {".md", ".txt", ".json", ".csv"}
UPLOAD_SUFFIX = {".md", ".txt", ".docx", ".doc", ".pdf", ".html", ".htm", ".png", ".jpg",
                 ".jpeg", ".gif", ".m4a", ".mp3", ".wav", ".aac", ".csv", ".json",
                 ".pptx", ".xlsx", ".zip"}
RAW_TYPES = {".html": "text/html; charset=utf-8", ".htm": "text/html; charset=utf-8",
             ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
             ".gif": "image/gif", ".pdf": "application/pdf", ".css": "text/css",
             ".js": "text/javascript", ".m4a": "audio/mp4", ".mp3": "audio/mpeg",
             ".wav": "audio/wav", ".svg": "image/svg+xml"}
JOBS_DIR = ROOT / ".workbench"
JOBS_LOG = JOBS_DIR / "jobs.jsonl"

JOBS = {}          # id -> job dict
JOBS_LOCK = threading.Lock()


# ---------------- 工具 ----------------

def now_iso():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def slug(name):
    """目录/文件名净化:保留中文,去掉危险字符。"""
    s = re.sub(r'[\\/:*?"<>|\'`\x00-\x1f]', "", str(name)).strip()
    s = re.sub(r"\s+", "-", s)
    return s[:80]


def safe_path(rel, must_exist=False):
    """把相对路径解析到 ROOT 内,越界抛 PermissionError。"""
    p = (ROOT / rel).resolve()
    try:
        p.relative_to(ROOT)
    except ValueError:
        raise PermissionError(f"路径越界: {rel}")
    if must_exist and not p.exists():
        raise FileNotFoundError(rel)
    return p


def writable_path(rel):
    p = safe_path(rel)
    r = str(p.relative_to(ROOT))
    # 技能与角色属于可编辑插件
    if re.match(r"^plugins/skills/[^/]+/SKILL\.md$", r) or r == "plugins/roles.json":
        return p
    parts = p.relative_to(ROOT).parts
    if not parts or parts[0] not in DATA_DIRS:
        raise PermissionError(f"只允许写入数据区 {sorted(DATA_DIRS)}: {rel}")
    if p.suffix.lower() not in EDIT_SUFFIX:
        raise PermissionError(f"只允许编辑文本文件 {sorted(EDIT_SUFFIX)}: {rel}")
    return p


# ---------------- 技能与专家角色 ----------------

SKILL_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,40}$")


def parse_skill_md(p):
    try:
        txt = p.read_text(encoding="utf-8")
    except OSError:
        return {"name": p.parent.name, "description": ""}
    meta = {}
    m = re.match(r"^---\s*\n(.*?)\n---", txt, re.S)
    if m:
        for line in m.group(1).splitlines():
            if ":" in line:
                k, v = line.split(":", 1)
                meta[k.strip()] = v.strip()
    return {"name": meta.get("name", p.parent.name),
            "description": meta.get("description", "")}


def scan_skills():
    out = []
    base = ROOT / "plugins" / "skills"
    if base.is_dir():
        for d in sorted(base.iterdir()):
            if d.is_dir() and (d / "SKILL.md").exists():
                # 兜底镜像:plugins 侧较新或镜像缺失时自动同步到 .claude/skills/
                # (AI 生成、手动拷入、git pull 的技能都靠这里自动生效)
                dst = ROOT / ".claude" / "skills" / d.name / "SKILL.md"
                try:
                    if not dst.exists() or \
                            (d / "SKILL.md").stat().st_mtime > dst.stat().st_mtime:
                        mirror_skill(d.name)
                except OSError:
                    pass
                info = parse_skill_md(d / "SKILL.md")
                info["name"] = d.name          # 目录名为唯一标识(导入的第三方技能 frontmatter 可能不一致)
                info["dir"] = d.name
                info["path"] = f"plugins/skills/{d.name}/SKILL.md"
                info["files"] = sum(1 for f in d.rglob("*") if f.is_file())
                out.append(info)
    return out


def mirror_skill(name):
    """整目录镜像到 .claude/skills/(第三方技能常带 references/scripts 附属文件)。"""
    src = ROOT / "plugins" / "skills" / name
    if src.is_dir() and (src / "SKILL.md").exists():
        shutil.copytree(src, ROOT / ".claude" / "skills" / name, dirs_exist_ok=True)


# ---------------- 回收站(删除 = 可恢复,不直接抹掉真实工作资产) ----------------

TRASH_DIR = ROOT / ".trash"
TRASH_INDEX = TRASH_DIR / "index.json"
TRASH_LOCK = threading.Lock()


def load_trash():
    return (wb.load_json(TRASH_INDEX) or {}).get("items", [])


def save_trash(items):
    TRASH_DIR.mkdir(exist_ok=True)
    TRASH_INDEX.write_text(json.dumps({"items": items}, ensure_ascii=False, indent=2) + "\n",
                           encoding="utf-8")


def move_to_trash(rel, kind="file", note=""):
    """把数据区内的文件/目录移入 .trash/,记录原路径以便恢复。"""
    p = safe_path(rel, must_exist=True)
    parts = p.relative_to(ROOT).parts
    if not parts or parts[0] not in DATA_DIRS | {"plugins"}:
        raise PermissionError(f"只允许删除数据区内容: {rel}")
    if len(parts) == 1:
        raise PermissionError("不能删除数据区顶层目录")
    tid = uuid.uuid4().hex[:10]
    with TRASH_LOCK:
        dest_dir = TRASH_DIR / tid
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / p.name
        shutil.move(str(p), str(dest))
        items = load_trash()
        items.insert(0, {"id": tid, "name": p.name, "origin": str(p.relative_to(ROOT)),
                         "kind": kind, "note": note, "deleted": now_iso(),
                         "is_dir": dest.is_dir(),
                         "size": sum(f.stat().st_size for f in dest.rglob("*") if f.is_file())
                         if dest.is_dir() else dest.stat().st_size})
        save_trash(items)
    return tid


def restore_from_trash(tid):
    with TRASH_LOCK:
        items = load_trash()
        it = next((x for x in items if x["id"] == tid), None)
        if not it:
            raise FileNotFoundError("回收站中不存在该项")
        src = TRASH_DIR / tid / it["name"]
        if not src.exists():
            raise FileNotFoundError("回收站文件已丢失")
        dst = ROOT / it["origin"]
        if dst.exists():
            raise ValueError(f"原位置已存在同名项,无法恢复: {it['origin']}")
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(src), str(dst))
        shutil.rmtree(TRASH_DIR / tid, ignore_errors=True)
        save_trash([x for x in items if x["id"] != tid])
    return it["origin"]


# ---------------- 模板库(系统默认 + 用户上传,按槽位多候选) ----------------

TPL_LOCK = threading.Lock()
USER_TPL_DIR = ROOT / "plugins" / "templates" / "_user"
USER_TPL_INDEX = USER_TPL_DIR / "index.json"
# 模板上传:除明确危险的可执行类外一律接受,能不能转由提取器决定
BLOCKED_UPLOAD = {".app", ".dmg", ".pkg", ".sh", ".command", ".exe", ".bat",
                  ".scpt", ".jar", ".py", ".js"}


def load_user_tpl():
    d = wb.load_json(USER_TPL_INDEX) or {}
    return d.get("templates", []), d.get("defaults", {})


def save_user_tpl(templates, defaults):
    USER_TPL_DIR.mkdir(parents=True, exist_ok=True)
    USER_TPL_INDEX.write_text(
        json.dumps({"templates": templates, "defaults": defaults},
                   ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def scan_templates(c):
    """每个槽位的模板候选:系统默认包 + 用户上传,标注标签与默认项。"""
    pid, pdir, manifest = active_pack(c)
    files = manifest.get("files", {})
    utpls, defaults = load_user_tpl()
    out = {}
    for step in c.get("pipeline", []) + [{"slot": "customer-profile", "label": "客户档案"}]:
        slot = step["slot"]
        if slot in out:
            continue
        cands = []
        rel = files.get(slot)
        if rel and (pdir / rel).exists():
            p = pdir / rel
            cands.append({"id": f"sys:{slot}", "name": Path(rel).name,
                          "tags": ["系统默认", pid], "source": "system",
                          "path": f"{c['templates']['dir']}/{pid}/{rel}",
                          "ext": p.suffix.lower(), "size": p.stat().st_size,
                          "editable": p.suffix.lower() in EDIT_SUFFIX})
        for t in utpls:
            if t.get("slot") != slot:
                continue
            p = USER_TPL_DIR / t["file"]
            if not p.exists():
                continue
            cands.append({"id": t["id"], "name": t.get("name") or p.stem,
                          "tags": (t.get("tags") or []) + ["我的模板"], "source": "user",
                          "path": str(p.relative_to(ROOT)), "ext": p.suffix.lower(),
                          "size": p.stat().st_size, "editable": True,
                          "origin_name": t.get("origin_name", ""),
                          "created": t.get("created", "")})
        dft = defaults.get(slot) or (cands[0]["id"] if cands else None)
        if not any(x["id"] == dft for x in cands):
            dft = cands[0]["id"] if cands else None
        for x in cands:
            x["is_default"] = (x["id"] == dft)
        out[slot] = {"label": step.get("label", slot), "candidates": cands, "default": dft}
    return out


def find_template(c, slot, tpl_id=None):
    """定位某槽位要用的模板文件(未指定则用默认)。"""
    tpls = scan_templates(c).get(slot)
    if not tpls or not tpls["candidates"]:
        raise FileNotFoundError(f"槽位 {slot} 没有可用模板 —— 请在「模板管理」上传一个")
    tid = tpl_id or tpls["default"]
    hit = next((x for x in tpls["candidates"] if x["id"] == tid), None) \
        or tpls["candidates"][0]
    return ROOT / hit["path"], hit


def convert_to_md(src_path, title, origin_name=None, keep_original=False):
    """用户上传的模板 → Markdown。
    keep_original=True 时表格类(xlsx)保留原格式直接复用;否则一律尽力转 md。"""
    suf = src_path.suffix.lower()
    shown = origin_name or src_path.name
    if suf in (".md", ".markdown", ".txt"):
        return src_path.read_text(encoding="utf-8", errors="replace"), ".md"
    if keep_original and suf in (".xlsx", ".xlsm"):
        return None, suf                       # 带公式的表格保留原样更有用
    sys.path.insert(0, str(BIN))
    import extract as ex
    fn = ex.EXTRACTORS.get(suf)
    if not fn:
        raise ValueError(
            f"暂不支持 {suf} 的自动转换。可用格式:"
            f"{', '.join(sorted(ex.EXTRACTORS))}。"
            f"老版 .doc/.xls/.ppt 请先在 Office 里另存为 .docx/.xlsx/.pptx;"
            f"Pages/Keynote 请导出为 PDF 或 Word")
    body = ex.clean(fn(src_path))
    if not body.strip():
        raise ValueError(
            "转换后内容为空 —— " +
            ("该 PDF 多半是扫描件/图片型(文字是图片),需先 OCR"
             if suf == ".pdf" else f"未能从 {suf} 中提取到任何文本"))
    # 只对 PDF 做"疑似扫描件"的长度判断,其他格式允许很短的骨架模板
    if suf == ".pdf" and len(body) < 20:
        raise ValueError("PDF 提取到的文字极少,多为扫描件/图片型,需先 OCR")
    return (f"---\nsource: {shown}\nconverted: "
            f"{datetime.now().strftime('%Y-%m-%d %H:%M')}\n---\n\n# {title}\n\n"
            f"> 由 {shown} 自动转换为 Markdown,可直接编辑\n\n{body}\n"), ".md"


# ---------------- 技能市场(第三方 skill 榜单 → 工作台本地技能) ----------------

_MARKET_CACHE = {"mtime": 0, "data": None}


def parse_market(c, force=False):
    """解析榜单 HTML 里的 const DATA = {...}(标准 JSON)。
    按源文件 mtime 缓存:源文件一更新,下次请求自动重新解析(force=True 可强制)。"""
    src = (c.get("skill_market") or {}).get("source") or ""
    if not src:
        return {"error": "未配置技能市场源(workbench.json → skill_market.source)"}
    p = Path(src).expanduser()
    if not p.is_file():
        return {"error": f"市场源文件不存在: {src}"}
    def with_installed(data):
        """已安装状态每次实时算 —— 不能随榜单一起缓存。"""
        have = {d.name for d in (ROOT / "plugins" / "skills").iterdir()
                if d.is_dir()} if (ROOT / "plugins" / "skills").is_dir() else set()
        skills = [dict(s, installed=s["id"] in have) for s in data["skills"]]
        return dict(data, skills=skills,
                    installed_count=sum(1 for s in skills if s["installed"]),
                    source=str(p),
                    source_updated=datetime.fromtimestamp(
                        p.stat().st_mtime).strftime("%Y-%m-%d %H:%M"))

    mt = p.stat().st_mtime
    if not force and _MARKET_CACHE["mtime"] == mt and _MARKET_CACHE["data"]:
        return with_installed(_MARKET_CACHE["data"])
    try:
        txt = p.read_text(encoding="utf-8", errors="replace")
        i = txt.index("const DATA = {")
        start = txt.index("{", i)
        depth = 0
        end = None
        for j in range(start, len(txt)):
            if txt[j] == "{":
                depth += 1
            elif txt[j] == "}":
                depth -= 1
                if depth == 0:
                    end = j + 1
                    break
        raw = json.loads(txt[start:end])
    except (ValueError, json.JSONDecodeError) as e:
        return {"error": f"解析市场源失败: {e}"[:160]}

    def zh(v):
        if isinstance(v, dict):
            return v.get("zh") or v.get("en") or ""
        return v or ""

    inds = {d["id"]: zh(d.get("name")) for d in raw.get("industries", [])}
    scen = {}
    for d in raw.get("industries", []):
        for s in d.get("scenarios", []):
            scen[s["id"]] = zh(s.get("name"))
    skills = []
    for s in raw.get("skills", []):
        sid = s.get("id", "")
        skills.append({
            "id": sid, "name": zh(s.get("name")), "capability": zh(s.get("capability")),
            "reason": zh(s.get("reason")),
            "industry": s.get("industry", ""), "industry_name": inds.get(s.get("industry"), ""),
            "scenario_name": scen.get(s.get("scenario"), ""),
            "stars": (s.get("signals") or {}).get("stars", 0),
            "url": s.get("url", ""), "repo": s.get("sourceRepo", ""),
            "compat": s.get("compat", []), "builtin": bool(s.get("builtin")),
        })
    skills.sort(key=lambda x: -(x["stars"] or 0))
    data = {"fetched": raw.get("fetchedAt", ""), "industries": inds, "skills": skills}
    _MARKET_CACHE.update({"mtime": mt, "data": data})
    return with_installed(data)


def gh_raw_candidates(url):
    """把 GitHub 页面 URL 换算成 raw SKILL.md 候选地址。"""
    m = re.match(r"https?://github\.com/([^/]+)/([^/#?]+)(?:/tree/([^/]+)/(.+?))?/?$", url or "")
    if not m:
        return []
    owner, repo, branch, sub = m.group(1), m.group(2), m.group(3), m.group(4)
    branches = [branch] if branch else ["main", "master"]
    subs = [sub] if sub else [""]
    out = []
    for b in branches:
        for s in subs:
            base = f"https://raw.githubusercontent.com/{owner}/{repo}/{b}/"
            out.append(base + (f"{s}/SKILL.md" if s else "SKILL.md"))
    return out


def fetch_skill_md(url, timeout=12):
    import urllib.error
    import urllib.request
    for cand in gh_raw_candidates(url):
        try:
            req = urllib.request.Request(cand, headers={"User-Agent": "presales-workbench"})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                if r.status == 200:
                    body = r.read(400_000).decode("utf-8", "replace")
                    if "---" in body[:200] or "#" in body[:400]:
                        return body, cand
        except (urllib.error.URLError, OSError, ValueError):
            continue
    return None, None


def market_skill_md(item, fetched_body, fetched_from):
    """生成落地的 SKILL.md:优先真实内容,附本地化说明头;拉不到则用榜单元数据生成精简版。"""
    head = (f"---\nname: {item['id']}\n"
            f"description: {item['name']} — {item['capability'][:120]}\n---\n\n")
    origin = (f"> **来源**:[{item['repo'] or item['url']}]({item['url']}) · "
              f"⭐{item['stars']} · 由工作台技能市场导入(本地化,不装到全局)\n"
              f"> **本地化说明**:本技能运行在本工作台 `plugins/skills/` 下。"
              f"若原技能依赖外部 MCP、命令行工具或全局配置,这些能力在此不可用——"
              f"遇到时按本工作台已有能力(本地文件、bin/ 脚本、已装 MCP)替代,不要假装调用不存在的工具。\n\n")
    if fetched_body:
        body = re.sub(r"^---\s*\n.*?\n---\s*\n", "", fetched_body, count=1, flags=re.S)
        return head + origin + f"<!-- 原文抓取自 {fetched_from} -->\n\n" + body.strip() + "\n"
    return (head + origin +
            f"# {item['name']}\n\n## 能力\n{item['capability']}\n\n"
            f"## 适用\n{item['industry_name']} · {item['scenario_name']}\n\n"
            f"## 入选理由\n{item['reason']}\n\n"
            f"## 说明\n未能自动抓取原仓库的 SKILL.md(可能是私有、路径变动或网络不可达)。"
            f"以上为榜单元数据生成的精简版:可先按「能力」描述使用,"
            f"需要完整指引时到 {item['url']} 查看原文,再用「导入第三方技能」上传。\n")


def load_roles():
    data = wb.load_json(ROOT / "plugins" / "roles.json") or {}
    return data.get("roles", [])


def save_roles(roles):
    (ROOT / "plugins").mkdir(exist_ok=True)
    (ROOT / "plugins" / "roles.json").write_text(
        json.dumps({"roles": roles}, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8")


def read_text(p, limit=2_000_000):
    return p.read_text(encoding="utf-8", errors="replace")[:limit]


def cfg():
    c = wb.load_json(ROOT / "workbench.json")
    if c is None:
        raise RuntimeError("找不到 workbench.json")
    return c


def active_pack(c):
    tdir = ROOT / c.get("templates", {}).get("dir", "plugins/templates")
    pid = c.get("templates", {}).get("active_pack", "")
    pdir = tdir / pid
    manifest = wb.load_json(pdir / "manifest.json") or {}
    return pid, pdir, manifest


# ---------------- 领域扫描 ----------------

def scan_customers():
    out = []
    base = ROOT / "customers"
    if not base.is_dir():
        return out
    for d in sorted(base.iterdir()):
        if not d.is_dir() or d.name.startswith("."):
            continue
        meetings = sorted((f.name for f in (d / "meetings").glob("*.md")), reverse=True) \
            if (d / "meetings").is_dir() else []
        out.append({
            "id": d.name,
            "meta": wb.load_json(d / "customer.json") or {},
            "profile": f"customers/{d.name}/profile.md" if (d / "profile.md").exists() else None,
            "meetings": [f"customers/{d.name}/meetings/{m}" for m in meetings],
        })
    return out


def scan_meetings():
    """全部纪要(按日期倒序)。纪要与项目绑定:projects/<dir>/meetings/;
    归档项目的纪要随项目留在 archive/;customers/<id>/meetings/ 为历史遗留,可归属到项目。"""
    out = []

    def add_from(pdir, kind):
        md = pdir / "meetings"
        if not md.is_dir():
            return
        meta = wb.load_json(pdir / "project.json") or {}
        for f in sorted(md.glob("*.md")):
            m = re.match(r"^(\d{4}-\d{2}-\d{2})-(.+)\.md$", f.name)
            out.append({"path": str(f.relative_to(ROOT)),
                        "date": m.group(1) if m else "", "topic": m.group(2) if m else f.stem,
                        "project": pdir.name, "customer": meta.get("customer", ""),
                        "kind": kind, "size": f.stat().st_size})

    for base, kind in ((ROOT / "projects", "project"), (ROOT / "archive", "archived")):
        if base.is_dir():
            for d in sorted(base.iterdir()):
                if d.is_dir() and not d.name.startswith((".", "_")):
                    add_from(d, kind)
    base = ROOT / "customers"
    if base.is_dir():
        for d in sorted(base.iterdir()):
            md = d / "meetings"
            if not d.is_dir() or d.name.startswith((".", "_")) or not md.is_dir():
                continue
            for f in sorted(md.glob("*.md")):
                m = re.match(r"^(\d{4}-\d{2}-\d{2})-(.+)\.md$", f.name)
                out.append({"path": str(f.relative_to(ROOT)),
                            "date": m.group(1) if m else "", "topic": m.group(2) if m else f.stem,
                            "project": "", "customer": d.name,
                            "kind": "unbound", "size": f.stat().st_size})
    out.sort(key=lambda x: (x["date"], x["path"]), reverse=True)
    return out


MEETING_TODO_RE = re.compile(r"^(\s*[-*+]\s+)\[([ xX])\]\s*(.+?)\s*$")


def scan_meeting_todos(meetings):
    """从纪要的行动项(- [ ] xxx)提取待办。
    task_index 与前端渲染的第 N 个复选框一致,勾选时才能精确回写同一行。"""
    out = []
    year = datetime.now().year
    for mt in meetings:
        p = ROOT / mt["path"]
        try:
            lines = p.read_text(encoding="utf-8", errors="replace").split("\n")
        except OSError:
            continue
        idx = 0
        for line in lines:
            m = MEETING_TODO_RE.match(line)
            if not m:
                continue
            text = m.group(3).strip()
            # 模板占位符(如「【事项】@【负责人】【截止】」)不算真待办,但索引仍要递增
            if len(re.sub(r"【[^】]*】", "", text).strip(" @·、")) < 2:
                idx += 1
                continue
            owner = ""
            mo = re.search(r"@\s*([^\s@,,;;]+)", text)
            if mo:
                owner = mo.group(1)
            due = ""
            md = re.search(r"(\d{4}-\d{1,2}-\d{1,2})|(?<!\d)(\d{1,2}-\d{1,2})(?!\d)", text)
            if md:
                d = md.group(0).split("-")
                try:
                    due = (f"{d[0]}-{int(d[1]):02d}-{int(d[2]):02d}" if len(d) == 3
                           else f"{year}-{int(d[0]):02d}-{int(d[1]):02d}")
                except ValueError:
                    due = ""
            out.append({"kind": "meeting", "id": f"{mt['path']}#{idx}",
                        "path": mt["path"], "task_index": idx,
                        "title": text, "owner": owner, "due": due,
                        "done": m.group(2).lower() == "x",
                        "project": mt["project"], "customer": mt["customer"],
                        "topic": mt["topic"], "meeting_date": mt["date"]})
            idx += 1
    return out


def toggle_meeting_todo(rel, index, checked):
    """精确改纪要里第 index 个行动项的勾选状态(不做整篇重写)。"""
    p = safe_path(rel, must_exist=True)
    lines = p.read_text(encoding="utf-8", errors="replace").split("\n")
    n, hit = 0, False
    for i, line in enumerate(lines):
        m = MEETING_TODO_RE.match(line)
        if not m:
            continue
        if n == index:
            lines[i] = f"{m.group(1)}[{'x' if checked else ' '}] {m.group(3)}"
            hit = True
            break
        n += 1
    if not hit:
        raise FileNotFoundError(f"纪要里找不到第 {index} 个行动项")
    p.write_text("\n".join(lines), encoding="utf-8")
    return str(p.relative_to(ROOT))


def scan_archived():
    """archive/ 下已归档项目:元数据 + 文件清单(供归档详情页查看)。"""
    out = []
    base = ROOT / "archive"
    if not base.is_dir():
        return out
    for d in sorted(base.iterdir()):
        if d.is_dir() and (d / "project.json").exists():
            files = [{"name": f.name, "size": f.stat().st_size,
                      "editable": f.suffix.lower() in EDIT_SUFFIX}
                     for f in sorted(d.iterdir())
                     if f.is_file() and not f.name.startswith(".")]
            out.append({"dir": d.name, "meta": wb.load_json(d / "project.json") or {},
                        "files": files})
    return out


def recent_files(limit=12, days=None):
    items = []
    cutoff = (datetime.now() - timedelta(days=days)).timestamp() if days else 0
    for top in ["customers", "projects"]:
        base = ROOT / top
        if not base.is_dir():
            continue
        for p in base.rglob("*"):
            if not p.is_file() or p.name.startswith("."):
                continue
            if p.name in ("project.json", "customer.json"):
                continue
            mt = p.stat().st_mtime
            if mt >= cutoff:
                items.append((mt, str(p.relative_to(ROOT))))
    items.sort(reverse=True)
    return [{"path": rel, "mtime": datetime.fromtimestamp(mt).strftime("%m-%d %H:%M")}
            for mt, rel in items[:limit]]


def make_weekly(c):
    """本地即时生成周报草稿(不耗 AI),保存到 knowledge/reports/。"""
    today = datetime.now()
    week_ago = today - timedelta(days=7)
    # 本周纪要(纪要与项目绑定)
    meets = []
    for mt in scan_meetings():
        if not mt["date"]:
            continue
        try:
            dt = datetime.strptime(mt["date"], "%Y-%m-%d")
        except ValueError:
            continue
        if dt >= week_ago:
            meets.append((mt["date"], mt["customer"] or "【未绑定客户】",
                          mt["topic"], mt["path"],
                          mt["project"] or "【未绑定项目】"))
    meets.sort(reverse=True)
    projects = scan_projects(c)
    archived = scan_archived()

    def wan(v):
        try:
            return float(v)
        except (TypeError, ValueError):
            return 0.0

    total_contract = sum(wan(p["meta"].get("contract_wan")) for p in projects) + \
        sum(wan(a["meta"].get("contract_wan")) for a in archived)
    new_deliv = recent_files(limit=30, days=7)
    new_deliv = [r for r in new_deliv if r["path"].startswith("projects/")]

    L = []
    L.append(f"# 售前周报 · {today.strftime('%Y-%m-%d')}(第 {today.isocalendar()[1]} 周)")
    L.append("")
    L.append(f"## 一、本周拜访与沟通({len(meets)} 次)")
    L += [f"- {d} · {cid} · {topic}(项目:{proj})"
          for d, cid, topic, _, proj in meets] or ["-(本周无新增纪要)"]
    L.append("")
    L.append("## 二、商机进展")
    if projects:
        L.append("| 项目 | 客户 | 阶段 | 预算(万) | 合同(万) | 交付物 |")
        L.append("|---|---|---|---|---|---|")
        for p in projects:
            m = p["meta"]
            L.append(f"| {m.get('name', p['dir'])} | {m.get('customer', '')} | {m.get('stage', '')} "
                     f"| {m.get('budget_wan', '') or '—'} | {m.get('contract_wan', '') or '—'} "
                     f"| {p['done']}/{p['total']} |")
    else:
        L.append("-(暂无进行中商机)")
    L.append("")
    L.append(f"## 三、本周新产出交付物({len(new_deliv)} 份)")
    L += [f"- {r['path']}({r['mtime']})" for r in new_deliv] or ["-(无)"]
    L.append("")
    L.append("## 四、签约与归档")
    L.append(f"- 累计签约合同额:**{total_contract:g} 万**(含归档 {len(archived)} 个项目)")
    L.append("")
    L.append("## 五、风险与需支持\n- 【待补充,可参考看板风险提示】")
    L.append("")
    L.append("## 六、下周计划\n- 【待补充】")
    md = "\n".join(L) + "\n"

    rdir = ROOT / "knowledge" / "reports"
    rdir.mkdir(parents=True, exist_ok=True)
    p = rdir / f"周报-{today.strftime('%Y-%m-%d')}.md"
    i = 2
    while p.exists():   # 绝不覆盖已有周报(可能是 AI 精修版)
        p = rdir / f"周报-{today.strftime('%Y-%m-%d')}-v{i}.md"
        i += 1
    p.write_text(md, encoding="utf-8")
    return str(p.relative_to(ROOT))


def slot_prefix(slot):
    m = re.match(r"^(\d{2})-", slot)
    return m.group(1) if m else None


def scan_projects(c):
    pipeline = [s for s in c.get("pipeline", []) if slot_prefix(s["slot"])]
    out = []
    base = ROOT / "projects"
    if not base.is_dir():
        return out
    for d in sorted(base.iterdir()):
        if not d.is_dir() or d.name.startswith(".") or d.name.startswith("_"):
            continue
        meta = wb.load_json(d / "project.json") or {}
        slots = []
        for s in pipeline:
            pref = slot_prefix(s["slot"])
            hits = sorted(f.name for f in d.iterdir()
                          if f.is_file() and f.name.startswith(pref + "-"))
            slots.append({"slot": s["slot"], "label": s["label"], "stage": s.get("stage", ""),
                          "files": [f"projects/{d.name}/{h}" for h in hits]})
        done = sum(1 for s in slots if s["files"])
        slotted = {Path(f).name for s in slots for f in s["files"]}
        others = [f"projects/{d.name}/{f.name}" for f in sorted(d.iterdir())
                  if f.is_file() and not f.name.startswith(".")
                  and f.name not in slotted and f.name != "project.json"]
        # 需要提取但还没有伴生文本的二进制交付物(AI 读不了原文)
        pending = [f.name for f in d.iterdir()
                   if f.is_file() and not f.name.startswith(".")
                   and f.suffix.lower() in EXTRACTABLE
                   and not (d / ".extracted" / (f.stem + ".md")).exists()]
        out.append({"dir": d.name, "meta": meta, "slots": slots, "others": others,
                    "pending_extract": pending,
                    "done": done, "total": len(slots)})
    return out


def scan_tree(rel):
    """浅层文件树(两级),供知识库/收件箱浏览。"""
    base = ROOT / rel
    items = []
    if not base.is_dir():
        return items
    for p in sorted(base.rglob("*")):
        if p.is_dir():
            continue
        r = p.relative_to(ROOT)
        # 跳过隐藏文件与隐藏目录(如 .extracted 提取产物:供搜索用,不进浏览树)
        if any(part.startswith(".") for part in r.parts):
            continue
        if len(r.parts) > 9:   # 支持 my local knowledge/ 下多级自定义文件夹
            continue
        items.append({"path": str(r), "name": p.name,
                      "dir": str(r.parent.relative_to(rel)) if str(r.parent) != rel else "",
                      "size": p.stat().st_size,
                      "editable": p.suffix.lower() in EDIT_SUFFIX})
    return items


def search_all(q, limit=200):
    q_low = q.lower()
    hits = []
    for top in ["customers", "projects", "knowledge", "inbox"]:
        base = ROOT / top
        if not base.is_dir():
            continue
        for p in base.rglob("*"):
            if len(hits) >= limit:
                return hits
            if not p.is_file() or p.suffix.lower() not in EDIT_SUFFIX or p.name.startswith("."):
                continue
            try:
                for i, line in enumerate(p.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
                    if q_low in line.lower():
                        hits.append({"path": str(p.relative_to(ROOT)), "line": i,
                                     "text": line.strip()[:160]})
                        if len(hits) >= limit:
                            break
            except OSError:
                continue
    return hits


# ---------------- 导出(md → Word/PDF) ----------------

def md2html(md_text):
    """Markdown→HTML,供 textutil 转 Word。
    **必须全部内联 style**:textutil 会丢弃 <style> 块,且 Word/WPS 会用自带的
    「标题1」样式(居中、超大)覆盖 h1,内联样式才压得住。视觉与前端预览保持一致。"""
    import html as _h

    S_BODY = ("font-family:'PingFang SC','Microsoft YaHei',-apple-system,Arial;"
              "font-size:11pt;line-height:1.75;color:#16181d;text-align:left")
    S_H = {1: "font-size:17pt;font-weight:700;margin:16pt 0 8pt;text-align:left;color:#16181d",
           2: "font-size:14pt;font-weight:700;margin:14pt 0 6pt;text-align:left;color:#16181d",
           3: "font-size:12pt;font-weight:700;margin:12pt 0 5pt;text-align:left;color:#16181d"}
    S_P = "margin:5pt 0;text-align:left"
    S_LI = "margin:2pt 0;text-align:left"
    S_UL = "margin:5pt 0 5pt 18pt;padding-left:0"
    S_TABLE = ("border-collapse:collapse;margin:8pt 0;width:100%;font-size:10pt")
    S_TH = ("border:1px solid #b9bec7;padding:4pt 7pt;text-align:left;"
            "background-color:#eef0f3;font-weight:700")
    S_TD = "border:1px solid #b9bec7;padding:4pt 7pt;text-align:left"
    S_QUOTE = ("margin:6pt 0;padding:3pt 10pt;border-left:3px solid #1f3d99;"
               "color:#5b6270;font-size:10.5pt")
    S_PRE = ("font-family:Menlo,Consolas,monospace;font-size:9.5pt;background-color:#f2f3f5;"
             "padding:7pt 9pt;margin:7pt 0;white-space:pre-wrap;line-height:1.5")
    S_CODE = ("font-family:Menlo,Consolas,monospace;font-size:9.5pt;"
              "background-color:#f2f3f5;padding:0 3pt")
    S_HR = "border:none;border-top:1px solid #d5d9e0;margin:12pt 0"

    def inline(s):
        s = _h.escape(s)
        s = re.sub(r"`([^`]+)`", rf'<span style="{S_CODE}">\1</span>', s)
        s = re.sub(r"\*\*([^*]+)\*\*", r"<b>\1</b>", s)
        s = re.sub(r"(?<![\w*])\*([^*\n]+)\*(?![\w*])", r"<i>\1</i>", s)
        s = re.sub(r"\[([^\]]+)\]\((https?://[^)]+)\)",
                   r'<a href="\2" style="color:#1f3d99">\1</a>', s)
        return s

    lines, out, in_list, in_code = md_text.split("\n"), [], None, False
    i = 0
    while i < len(lines):
        L = lines[i]
        if in_code:
            if L.startswith("```"):
                out.append("</div>")
                in_code = False
            else:
                out.append(_h.escape(L) + "<br>")
            i += 1
            continue
        if L.startswith("```"):
            out.append(f'<div style="{S_PRE}">')
            in_code = True
            i += 1
            continue
        if re.match(r"^\s*$", L):
            if in_list:
                out.append(f"</{in_list}>")
                in_list = None
            i += 1
            continue
        m = re.match(r"^(#{1,6})\s+(.*)", L)
        if m:
            if in_list:
                out.append(f"</{in_list}>")
                in_list = None
            n = min(len(m.group(1)), 3)
            out.append(f'<p style="{S_H[n]}">{inline(m.group(2))}</p>')
            i += 1
            continue
        if re.match(r"^([-*_]\s*){3,}$", L):
            out.append(f'<hr style="{S_HR}">')
            i += 1
            continue
        if L.startswith(">"):
            if in_list:
                out.append(f"</{in_list}>")
                in_list = None
            quote = [inline(L.lstrip("> "))]
            while i + 1 < len(lines) and lines[i + 1].startswith(">"):
                i += 1
                quote.append(inline(lines[i].lstrip("> ")))
            out.append(f'<div style="{S_QUOTE}">' + "<br>".join(quote) + "</div>")
            i += 1
            continue
        if "|" in L and i + 1 < len(lines) and re.match(r"^\s*\|?[\s:|-]+\|[\s:|-]*$", lines[i + 1]):
            if in_list:
                out.append(f"</{in_list}>")
                in_list = None
            cells = lambda r: [inline(c.strip()) for c in r.strip().strip("|").split("|")]  # noqa: E731
            out.append(f'<table style="{S_TABLE}" cellspacing="0"><tr>' +
                       "".join(f'<td style="{S_TH}">{c}</td>' for c in cells(L)) + "</tr>")
            i += 2
            while i < len(lines) and "|" in lines[i] and not re.match(r"^\s*$", lines[i]):
                out.append("<tr>" + "".join(f'<td style="{S_TD}">{c}</td>'
                                            for c in cells(lines[i])) + "</tr>")
                i += 1
            out.append("</table>")
            continue
        mu = re.match(r"^(\s*)[-*+]\s+(.*)", L)
        mo = re.match(r"^(\s*)\d+[.、]\s+(.*)", L)
        if mu or mo:
            g = mu or mo
            want = "ul" if mu else "ol"
            indent = len(g.group(1))
            body = g.group(2)
            body = re.sub(r"^\[\s\]\s*", "☐ ", body)
            body = re.sub(r"^\[x\]\s*", "☑ ", body, flags=re.I)
            if in_list != want:
                if in_list:
                    out.append(f"</{in_list}>")
                out.append(f'<{want} style="{S_UL}">')
                in_list = want
            pad = f";margin-left:{indent * 6}pt" if indent else ""
            out.append(f'<li style="{S_LI}{pad}">{inline(body)}</li>')
            i += 1
            continue
        out.append(f'<p style="{S_P}">{inline(L)}</p>')
        i += 1
    if in_list:
        out.append(f"</{in_list}>")
    if in_code:
        out.append("</div>")
    return (f'<html><head><meta charset="utf-8"></head>'
            f'<body style="{S_BODY}">' + "\n".join(out) + "</body></html>")


EXTRACTABLE = {".pdf", ".docx", ".doc", ".pptx", ".xlsx", ".xlsm", ".rtf", ".odt",
               ".html", ".htm", ".csv"}


def extract_sidecar(file_path):
    """给二进制交付物生成同名 .md 伴生文本,落到同目录的 .extracted/ 下。
    AI 的 Read 工具读不了 docx/xlsx/pptx,预先提取才能被可靠引用(否则它可能靠文件名瞎猜)。"""
    p = Path(file_path)
    if p.suffix.lower() not in EXTRACTABLE or not p.is_file():
        return None
    sys.path.insert(0, str(BIN))
    import extract as ex
    fn = ex.EXTRACTORS.get(p.suffix.lower())
    if not fn:
        return None
    try:
        body = ex.clean(fn(p))
    except Exception:  # noqa: BLE001
        return None
    if len(body.strip()) < 10:
        return None
    out_dir = p.parent / ".extracted"
    out_dir.mkdir(exist_ok=True)
    out = out_dir / (p.stem + ".md")
    out.write_text(
        f"---\nsource: {p.name}\nextracted: {now_iso()}\nchars: {len(body)}\n---\n\n"
        f"> 由 `{p.name}` 自动提取的纯文本(供 AI 读取;原文以 {p.suffix} 为准)\n\n"
        + body[:300_000] + "\n", encoding="utf-8")
    return str(out.relative_to(ROOT))


def extract_dir_sidecars(d):
    """批量补齐某目录下所有可提取文件的伴生文本,返回 (新增, 跳过)。"""
    made, skipped = 0, 0
    for f in sorted(Path(d).iterdir()):
        if not f.is_file() or f.name.startswith("."):
            continue
        if f.suffix.lower() not in EXTRACTABLE:
            continue
        side = f.parent / ".extracted" / (f.stem + ".md")
        if side.exists() and side.stat().st_mtime >= f.stat().st_mtime:
            skipped += 1
            continue
        if extract_sidecar(f):
            made += 1
    return made, skipped


def print_page(rel):
    """打印优化的 HTML(A4 版式)——浏览器 ⌘P「存储为 PDF」即得高质量中文 PDF。
    比服务端 PDF 库可靠:无需 cairo/pango 等系统库,字体与排版由浏览器负责。"""
    src = safe_path(rel, must_exist=True)
    if src.suffix.lower() != ".md":
        raise ValueError("仅支持打印 Markdown 文件")
    body = md2html(src.read_text(encoding="utf-8", errors="replace"))
    inner = re.sub(r"^.*?<body[^>]*>|</body>.*$", "", body, flags=re.S)
    title = src.stem
    return f"""<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<title>{title}</title>
<style>
@page {{ size: A4; margin: 18mm 16mm; }}
body {{ font-family:'PingFang SC','Microsoft YaHei',-apple-system,Arial;
  font-size:10.5pt; line-height:1.75; color:#16181d; margin:0; }}
table {{ border-collapse:collapse; width:100%; font-size:9.5pt; margin:8pt 0;
  page-break-inside:auto; }}
tr {{ page-break-inside:avoid; page-break-after:auto; }}
td,th {{ border:1px solid #b9bec7; padding:4pt 7pt; text-align:left;
  vertical-align:top; }}
p[style*="17pt"], p[style*="14pt"] {{ page-break-after:avoid; }}
ul,ol {{ margin:5pt 0 5pt 18pt; padding-left:0; }}
li {{ margin:2pt 0; }}
img {{ max-width:100%; }}
.__toolbar {{ position:fixed; top:0; left:0; right:0; background:#1f3d99; color:#fff;
  padding:10px 16px; font-size:13px; display:flex; gap:12px; align-items:center; z-index:99 }}
.__toolbar button {{ background:#fff; color:#1f3d99; border:none; border-radius:6px;
  padding:5px 14px; font-size:13px; font-weight:600; cursor:pointer }}
.__wrap {{ padding-top:52px }}
@media print {{ .__toolbar {{ display:none }} .__wrap {{ padding-top:0 }} }}
</style></head><body>
<div class="__toolbar">
  <b>{title}</b>
  <span style="flex:1"></span>
  <span style="opacity:.85">在打印对话框里选「目标:另存为 PDF」</span>
  <button onclick="window.print()">打印 / 存为 PDF</button>
</div>
<div class="__wrap">{inner}</div>
<script>window.addEventListener('load',()=>setTimeout(()=>window.print(),400));</script>
</body></html>"""


def export_doc(rel, fmt):
    src = safe_path(rel, must_exist=True)
    if src.suffix.lower() != ".md":
        raise ValueError("目前仅支持导出 Markdown 文件")
    if fmt == "docx":
        # 优先 python-docx:能生成真正的 Word 表格(textutil 会把 <table> 拆成段落)
        dst = src.with_suffix(".docx")
        try:
            sys.path.insert(0, str(BIN))
            import md2docx
            md2docx.convert(src, dst)
            return str(dst.relative_to(ROOT))
        except ImportError:
            pass    # 没装 python-docx 时退回 textutil(表格会退化为文本)

    html_txt = md2html(src.read_text(encoding="utf-8", errors="replace"))
    tmp_html = src.with_suffix(".export.html")
    tmp_html.write_text(html_txt, encoding="utf-8")
    try:
        if fmt == "docx":
            dst = src.with_suffix(".docx")
            r = subprocess.run(["textutil", "-convert", "docx", "-output", str(dst),
                                str(tmp_html)], capture_output=True, text=True, timeout=60)
            if r.returncode != 0 or not dst.exists():
                raise RuntimeError(f"textutil 转换失败: {r.stderr[:200]}")
            return str(dst.relative_to(ROOT))
        if fmt == "html":
            dst = src.with_suffix(".html")
            dst.write_text(print_page(rel), encoding="utf-8")
            return str(dst.relative_to(ROOT))
        raise ValueError(f"不支持的格式 {fmt}")
    finally:
        tmp_html.unlink(missing_ok=True)


# ---------------- 日历与提醒 ----------------

CAL_LOCK = threading.Lock()


def load_events():
    return (wb.load_json(ROOT / "calendar" / "events.json") or {}).get("events", [])


def save_events(events):
    (ROOT / "calendar").mkdir(exist_ok=True)
    (ROOT / "calendar" / "events.json").write_text(
        json.dumps({"events": events}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def notify_mac(title, body):
    subprocess.run(["osascript", "-e",
                    'display notification "{}" with title "{}" sound name "Glass"'.format(
                        body.replace('"', "'")[:120], title.replace('"', "'")[:60])],
                   capture_output=True, timeout=10)


def reminder_loop():
    while True:
        try:
            now = datetime.now()
            with CAL_LOCK:
                events = load_events()
                dirty = False
                for e in events:
                    if e.get("done") or e.get("notified"):
                        continue
                    try:
                        dt = datetime.strptime(f"{e['date']} {e.get('time', '09:00')}",
                                               "%Y-%m-%d %H:%M")
                    except (KeyError, ValueError):
                        continue
                    remind_at = dt - timedelta(minutes=int(e.get("remind_min", 30) or 0))
                    if remind_at <= now < dt + timedelta(minutes=5):
                        notify_mac("售前工作台 · 日程提醒",
                                   f"{e.get('time', '')} {e.get('title', '')}"
                                   f"{'(' + e.get('place', '') + ')' if e.get('place') else ''}")
                        e["notified"] = True
                        dirty = True
                if dirty:
                    save_events(events)
        except Exception:  # noqa: BLE001
            pass
        time.sleep(60)


# ---------------- 协作动作(售前 × 销售/产研/交付) ----------------

COLLAB_LOCK = threading.Lock()


def load_collab():
    return (wb.load_json(ROOT / "collab" / "collab.json") or {}).get("items", [])


def save_collab(items):
    (ROOT / "collab").mkdir(exist_ok=True)
    (ROOT / "collab" / "collab.json").write_text(
        json.dumps({"items": items}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


# ---------------- 自定义待办(看板 To Do) ----------------

TODO_LOCK = threading.Lock()


def load_todos():
    return (wb.load_json(ROOT / "calendar" / "todos.json") or {}).get("todos", [])


def save_todos(items):
    (ROOT / "calendar").mkdir(exist_ok=True)
    (ROOT / "calendar" / "todos.json").write_text(
        json.dumps({"todos": items}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


# ---------------- 定时 AI 任务 ----------------

SCHED_LOCK = threading.Lock()
SCHED_FILE = JOBS_DIR / "schedules.json"


def load_schedules():
    return (wb.load_json(SCHED_FILE) or {}).get("schedules", [])


def save_schedules(items):
    JOBS_DIR.mkdir(exist_ok=True)
    SCHED_FILE.write_text(json.dumps({"schedules": items}, ensure_ascii=False, indent=2) + "\n",
                          encoding="utf-8")


def scheduler_loop():
    while True:
        try:
            now = datetime.now()
            stamp_min = now.strftime("%Y-%m-%d %H:%M")
            with SCHED_LOCK:
                items = load_schedules()
                dirty = False
                for s in items:
                    if not s.get("enabled"):
                        continue
                    f = s.get("freq", {})
                    due = False
                    if f.get("type") == "daily":
                        due = now.strftime("%H:%M") == f.get("time") and \
                            (s.get("last_run", "")[:10] != now.strftime("%Y-%m-%d"))
                    elif f.get("type") == "weekly":
                        due = now.isoweekday() == int(f.get("weekday", 1)) and \
                            now.strftime("%H:%M") == f.get("time") and \
                            (s.get("last_run", "")[:10] != now.strftime("%Y-%m-%d"))
                    elif f.get("type") == "once":
                        due = f.get("when") == stamp_min and not s.get("last_run")
                    if due and claude_available():
                        try:
                            submit_job(cfg(), f"[定时] {s.get('title', '')}", s.get("prompt", ""))
                            s["last_run"] = stamp_min
                            dirty = True
                        except Exception:  # noqa: BLE001
                            pass
                if dirty:
                    save_schedules(items)
        except Exception:  # noqa: BLE001
            pass
        time.sleep(30)


# ---------------- 邮件 ----------------

def email_cfg():
    return wb.load_json(ROOT / ".workbench" / "email.json")


def send_mail(to_addrs, subject, body, attach_rels):
    conf = email_cfg()
    if not conf or not conf.get("smtp_host"):
        raise RuntimeError("未配置邮件:请在「纪要」页点「邮件设置」填写 SMTP 信息(建议用授权码)")
    import smtplib
    from email.mime.multipart import MIMEMultipart
    from email.mime.text import MIMEText
    from email.mime.application import MIMEApplication
    from email.header import Header
    from email.utils import formataddr

    msg = MIMEMultipart()
    sender = conf.get("from") or conf.get("smtp_user")
    msg["From"] = formataddr((str(Header("售前工作台", "utf-8")), sender))
    msg["To"] = ", ".join(to_addrs)
    msg["Subject"] = Header(subject, "utf-8")
    msg.attach(MIMEText(body or "见附件。", "plain", "utf-8"))
    for rel in attach_rels or []:
        p = safe_path(rel, must_exist=True)
        part = MIMEApplication(p.read_bytes())
        part.add_header("Content-Disposition", "attachment",
                        filename=Header(p.name, "utf-8").encode())
        msg.attach(part)
    port = int(conf.get("smtp_port", 465))
    if conf.get("use_ssl", True):
        srv = smtplib.SMTP_SSL(conf["smtp_host"], port, timeout=30)
    else:
        srv = smtplib.SMTP(conf["smtp_host"], port, timeout=30)
        srv.starttls()
    try:
        srv.login(conf["smtp_user"], conf["smtp_pass"])
        srv.sendmail(sender, to_addrs, msg.as_string())
    finally:
        srv.quit()


# ---------------- 场景库 ----------------

def extract_stats():
    """文档提取与 wiki 编译概况(知识库页展示)。"""
    src = ROOT / "knowledge" / "my local knowledge"
    exts = {".pdf", ".docx", ".doc", ".pptx", ".xlsx", ".rtf"}
    binary = 0
    if src.is_dir():
        binary = sum(1 for p in src.rglob("*")
                     if p.is_file() and not p.name.startswith(".")
                     and p.suffix.lower() in exts)
    man = wb.load_json(ROOT / "knowledge" / ".extracted" / "_manifest.json") or {}
    ok = sum(1 for v in man.values() if v.get("status") == "ok")
    failed = [k for k, v in man.items() if v.get("status") != "ok"]
    chars = sum(v.get("chars", 0) for v in man.values() if v.get("status") == "ok")
    wiki_dir = ROOT / "knowledge" / "wiki"
    wiki_pages = sum(1 for p in wiki_dir.rglob("*.md")
                     if p.is_file() and p.name != "index.md") if wiki_dir.is_dir() else 0
    return {"binary_docs": binary, "extracted": ok, "failed": len(failed),
            "failed_list": [Path(f).name for f in failed[:12]],
            "chars": chars, "wiki_pages": wiki_pages,
            "wiki_index": (wiki_dir / "index.md").exists()}


def scan_scenarios():
    out = []
    base = ROOT / "scenarios"
    if not base.is_dir():
        return out
    for ind in sorted(base.iterdir()):
        if not ind.is_dir() or ind.name.startswith("."):
            continue
        for sc in sorted(ind.iterdir()):
            if not sc.is_dir() or sc.name.startswith("."):
                continue
            meta = wb.load_json(sc / "scenario.json") or {}
            out.append({
                "industry": ind.name, "name": sc.name,
                "description": meta.get("description", ""),
                "tags": meta.get("tags", []),
                "demo": f"scenarios/{ind.name}/{sc.name}/demo.html"
                        if (sc / "demo.html").exists() else None,
                "dir": f"scenarios/{ind.name}/{sc.name}",
            })
    return out


# ---------------- AI 任务(claude CLI) ----------------

PERMISSION_MODES = ["bypassPermissions", "acceptEdits", "plan"]

DEFAULT_AI_COMMAND = ["claude", "-p", "{prompt}",
                      "--permission-mode", "{permission_mode}",
                      "--output-format", "json"]

# 模型别名由 claude CLI 的 --model 消化(见 `claude --help`),留空 = 跟随 CLI 自身默认
MODEL_PRESETS = [
    {"id": "", "label": "跟随 CLI 默认", "note": "不加 --model,用你 claude 里设的模型"},
    {"id": "opus", "label": "Opus 5", "note": "最强,长任务/复杂方案首选"},
    {"id": "sonnet", "label": "Sonnet 5", "note": "均衡,日常产出够用"},
    {"id": "fable", "label": "Fable 5", "note": "快,适合改写/短任务"},
    {"id": "haiku", "label": "Haiku 4.5", "note": "最省,适合批量小活"},
]

# 供应商 = 一组注入给 claude CLI 的环境变量(Anthropic 兼容协议)。
# base_url 走各家自己的 /anthropic 兼容端点;能力保留取决于该端点的成熟度。
PROVIDER_PRESETS = [
    {"id": "anthropic", "label": "Anthropic 官方", "base_url": "", "model": "",
     "needs_key": False,
     "note": "用本机 claude login 的登录态,不需要填 key。"},
    {"id": "deepseek", "label": "DeepSeek", "needs_key": True,
     "base_url": "https://api.deepseek.com/anthropic", "model": "deepseek-chat",
     "note": "key 在 platform.deepseek.com 申请;推理型可填 deepseek-reasoner。"},
    {"id": "kimi", "label": "Kimi(月之暗面)", "needs_key": True,
     "base_url": "https://api.moonshot.cn/anthropic", "model": "kimi-k2-turbo-preview",
     "note": "key 在 platform.moonshot.cn 申请;模型名以其控制台为准。"},
    {"id": "custom", "label": "自定义(任意 Anthropic 兼容网关)", "needs_key": True,
     "base_url": "", "model": "",
     "note": "填你自己的 BASE_URL + key + 模型名,如自建中转或公司网关。"},
]
PROVIDERS_FILE = JOBS_DIR / "providers.json"
# 切官方时必须清空的第三方变量,否则会串到官方端点上
THIRD_PARTY_ENV = ("ANTHROPIC_BASE_URL", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_MODEL",
                   "ANTHROPIC_SMALL_FAST_MODEL", "ANTHROPIC_DEFAULT_OPUS_MODEL",
                   "ANTHROPIC_DEFAULT_SONNET_MODEL", "ANTHROPIC_DEFAULT_HAIKU_MODEL")


def load_providers():
    """凭证只落 .workbench/providers.json(已 gitignore),不进 workbench.json、不进分发包。"""
    d = wb.load_json(PROVIDERS_FILE) or {}
    if not isinstance(d, dict):
        d = {}
    d.setdefault("active", "anthropic")
    d.setdefault("items", {})
    return d


def save_providers(d):
    JOBS_DIR.mkdir(parents=True, exist_ok=True)
    PROVIDERS_FILE.write_text(json.dumps(d, ensure_ascii=False, indent=2) + "\n",
                              encoding="utf-8")
    try:
        os.chmod(PROVIDERS_FILE, 0o600)   # 含 API key,只给自己读
    except OSError:
        pass


def provider_by_id(pid, d=None):
    """预设 + 用户覆盖 合并成一个完整供应商配置。
    d 传入未落盘的配置时用它校验 —— 「填 key + 同时启用」是一次请求,
    再去读磁盘会看不到刚填的 key。"""
    base = next((dict(p) for p in PROVIDER_PRESETS if p["id"] == pid), None)
    if base is None:
        base = {"id": pid, "label": pid, "base_url": "", "model": "",
                "needs_key": True, "note": ""}
    saved = ((d or load_providers()).get("items") or {}).get(pid) or {}
    for k in ("base_url", "model", "token", "small_model"):
        if saved.get(k) is not None:
            base[k] = saved[k]
    return base


def active_provider():
    return provider_by_id(load_providers().get("active") or "anthropic")


def is_third_party(p):
    return bool(p.get("id") != "anthropic" and (p.get("base_url") or "").strip())


def mask_token(t):
    t = (t or "").strip()
    if not t:
        return ""
    return (t[:6] + "…" + t[-4:]) if len(t) > 12 else "已配置"


def providers_public():
    """给前端的供应商视图:token 一律脱敏,绝不回传明文。"""
    d = load_providers()
    out = []
    for p in PROVIDER_PRESETS:
        m = provider_by_id(p["id"])
        out.append({"id": m["id"], "label": m["label"], "note": m.get("note", ""),
                    "base_url": m.get("base_url", ""), "model": m.get("model", ""),
                    "needs_key": m.get("needs_key", True),
                    "token_masked": mask_token(m.get("token")),
                    "configured": bool(not m.get("needs_key") or m.get("token"))})
    return {"active": d.get("active") or "anthropic", "items": out}


def claude_available():
    return shutil.which("claude") is not None


def job_env(p=None):
    """AI 子进程的环境:剥掉 Claude Code 自身的会话变量,再按供应商注入。"""
    env = dict(os.environ)
    for k in ("CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT", "CLAUDE_CODE_SSE_PORT"):
        env.pop(k, None)
    for k in THIRD_PARTY_ENV:
        env.pop(k, None)          # 先清干净,避免上一次切换的残留串味
    p = active_provider() if p is None else p
    if is_third_party(p):
        env["ANTHROPIC_BASE_URL"] = p["base_url"].strip()
        tok = (p.get("token") or "").strip()
        if tok:
            env["ANTHROPIC_AUTH_TOKEN"] = tok
            env.pop("ANTHROPIC_API_KEY", None)   # 两者并存时行为不确定,只留一个
        mdl = (p.get("model") or "").strip()
        if mdl:
            env["ANTHROPIC_MODEL"] = mdl
            env["ANTHROPIC_SMALL_FAST_MODEL"] = (p.get("small_model") or mdl).strip()
    return env


def ai_command(c, prompt, p=None, resume=None):
    ai = c.get("ai", {})
    tpl = ai.get("command") or DEFAULT_AI_COMMAND
    mode = ai.get("permission_mode") or "bypassPermissions"
    if mode not in PERMISSION_MODES:
        mode = "bypassPermissions"
    p = active_provider() if p is None else p
    # 第三方端点没有 opus/sonnet 这些别名,模型改由 ANTHROPIC_MODEL 指定
    model = "" if is_third_party(p) else (ai.get("model") or "").strip()
    out = []
    for a in tpl:
        if a == "{prompt}":
            out.append(prompt)
        elif a == "{permission_mode}":
            out.append(mode)
        elif a == "{model}":
            out.append(model)
        else:
            out.append(a)
    # 模型留空时,连带丢掉模板里的 --model 开关,避免传一个空参数
    cleaned, i = [], 0
    while i < len(out):
        if out[i] == "--model" and (i + 1 >= len(out) or not out[i + 1]):
            i += 2
            continue
        cleaned.append(out[i])
        i += 1
    if model and "--model" not in cleaned:
        cleaned += ["--model", model]
    # 续话:把上一轮的 session_id 接上,claude 会带着完整上下文继续
    if resume and "--resume" not in cleaned and "-r" not in cleaned:
        cleaned += ["--resume", str(resume)]
    return cleaned


def parse_cli_json(out):
    """解析 `claude --output-format json` 的结果信封。
    自定义 CLI(workbench.json 可换)输出纯文本时返回 None,调用方走原文本路径。"""
    s = (out or "").strip()
    if not (s.startswith("{") and s.endswith("}")):
        return None
    try:
        d = json.loads(s)
    except json.JSONDecodeError:
        return None
    return d if isinstance(d, dict) and "result" in d else None


def auth_hint(msg, env=None):
    """失败信息里认证类错误的人话提示 —— 官方与第三方供应商的排查方向不同。"""
    low = (msg or "").lower()
    authy = ("oauth" in low or "authenticate" in low or "unauthorized" in low
             or "401" in low or "invalid api key" in low or "api key" in low)
    if not authy:
        return ""
    if env and env.get("ANTHROPIC_BASE_URL"):
        return (f"第三方供应商({env['ANTHROPIC_BASE_URL']})认证失败:"
                "请在「AI 任务 → 模型与供应商」检查 key 是否有效、模型名是否正确、账户是否有余额。")
    return "本机 claude CLI 登录态失效:请在终端运行 `claude login` 重新登录后重试。"


def run_job(job_id, cmd, env=None):
    env = job_env() if env is None else env
    extra = {}          # 超时/找不到 CLI 时也要有,否则下面 update 会炸
    with JOBS_LOCK:
        JOBS[job_id]["status"] = "running"
        JOBS[job_id]["started"] = now_iso()
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, cwd=str(ROOT),
                           env=env, timeout=1800, stdin=subprocess.DEVNULL)
        ok = r.returncode == 0
        strip_ansi = lambda s: re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "", s)  # noqa: E731
        out = strip_ansi((r.stdout or "")).strip()
        err = strip_ansi((r.stderr or "")).strip()
        status = "done" if ok else "failed"
        hint = ""
        env_j = parse_cli_json(out)
        if env_j is not None:                    # JSON 信封:取正文,顺带拿 session/成本
            extra = {"session_id": env_j.get("session_id") or "",
                     "cost_usd": env_j.get("total_cost_usd") or 0,
                     "turns": env_j.get("num_turns") or 0,
                     "duration_ms": env_j.get("duration_ms") or 0}
            out = str(env_j.get("result") or "").strip()
            if env_j.get("is_error"):            # CLI 退出码 0 但内部报错
                ok, status = False, "failed"
        if not ok:
            hint = auth_hint(err + "\n" + out, env)
        result = out if ok else (err or out or "无输出")
    except subprocess.TimeoutExpired:
        status, result, hint = "failed", "任务超时(30 分钟)", ""
    except FileNotFoundError:
        status, result, hint = "failed", "找不到 claude CLI", "请先安装 Claude Code CLI 并登录。"
    with JOBS_LOCK:
        if job_id not in JOBS:      # 运行途中被删掉了,别把记录又写回来
            return
        JOBS[job_id].update({"status": status, "ended": now_iso(),
                             "output": result[-8000:], "hint": hint,
                             "artifacts": extract_artifacts(result)})
        JOBS[job_id].update(extra)
        rec = dict(JOBS[job_id])
    try:
        JOBS_DIR.mkdir(exist_ok=True)
        with open(JOBS_LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except OSError:
        pass


def save_jobs_log():
    """删除/归档后重写日志 —— 日志本是追加式的,不重写下次启动会把删掉的任务读回来。"""
    with JOBS_LOCK:
        recs = sorted(JOBS.values(), key=lambda j: j.get("created", ""))
        body = "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in recs)
    try:
        JOBS_DIR.mkdir(exist_ok=True)
        tmp = JOBS_LOG.with_suffix(".tmp")
        tmp.write_text(body, encoding="utf-8")
        tmp.replace(JOBS_LOG)       # 原子替换,中途崩掉不会留下半截日志
    except OSError:
        pass


JOB_CATEGORIES = {
    "deliverable": "交付物", "minutes": "纪要周报", "wiki": "知识编译",
    "extract": "文档提取", "skill": "技能", "collab": "客户情报",
    "scheduled": "定时任务", "other": "其他",
}


def guess_category(title):
    t = title or ""
    if t.startswith("[定时]"):
        return "scheduled"
    for kw, cat in (("提取知识库", "extract"), ("编译知识 wiki", "wiki"),
                    ("周报", "minutes"), ("纪要", "minutes"),
                    ("技能", "skill"), ("整理客户情报", "collab"),
                    ("拜访", "collab"), ("生成", "deliverable")):
        if kw in t:
            return cat
    return "other"


ARTIFACT_RE = re.compile(
    r"(?:customers|projects|knowledge|scenarios|calendar|archive|inbox|plugins)"
    r"/[^\s，,。;;、'\"`()()\[\]<>*|]+"
    r"\.(?:md|xlsx|docx|pdf|pptx|html|csv|json|txt)")


def extract_artifacts(text, limit=12):
    """从任务输出里认出真实存在的产出文件,供界面直接跳转。"""
    out = []
    for m in ARTIFACT_RE.findall(text or ""):
        rel = m.strip().rstrip(".,;:")
        if rel in out:
            continue
        try:
            p = safe_path(rel)
        except (PermissionError, ValueError):
            continue
        if p.is_file():
            out.append(rel)
        if len(out) >= limit:
            break
    return out


def chat_jobs(chat_id):
    """一条对话 = 共享 chat_id 的任务,按创建时间排序。"""
    with JOBS_LOCK:
        js = [dict(j) for j in JOBS.values() if j.get("chat_id") == chat_id]
    return sorted(js, key=lambda j: (j.get("created") or "", j.get("id") or ""))


def chat_list(limit=40):
    """对话列表:按最后活动倒序,标题取首轮。"""
    with JOBS_LOCK:
        js = [dict(j) for j in JOBS.values() if j.get("chat_id")]
    by = {}
    for j in sorted(js, key=lambda x: (x.get("created") or "", x.get("id") or "")):
        cid = j["chat_id"]
        c = by.setdefault(cid, {"id": cid, "title": j.get("title") or "新对话",
                                "created": j.get("created", ""), "turns": 0,
                                "cost_usd": 0, "provider": j.get("provider", "")})
        c["turns"] += 1
        c["cost_usd"] = round(c["cost_usd"] + (j.get("cost_usd") or 0), 4)
        c["last"] = j.get("created", "")
        c["running"] = j.get("status") in ("queued", "running")
    return sorted(by.values(), key=lambda x: x.get("last") or "", reverse=True)[:limit]


def resume_target(chat_id, provider_label):
    """取该对话最后一次成功的 session_id 用于续话。
    供应商换过就不续 —— session 存在各自的历史里,跨供应商 resume 必然失败。"""
    if not chat_id:
        return None, ""
    prev = [j for j in chat_jobs(chat_id) if j.get("session_id")]
    if not prev:
        return None, ""
    last = prev[-1]
    if (last.get("provider") or "") != (provider_label or ""):
        return None, f"供应商已从「{last.get('provider')}」换成「{provider_label}」,本轮重新开始上下文。"
    return last["session_id"], ""


def submit_job(c, title, prompt, category=None, chat_id=None):
    job_id = uuid.uuid4().hex[:12]
    # 供应商取一次快照:命令与环境必须来自同一份配置,否则中途切换会命令/key 错配
    p = active_provider()
    resume, note = resume_target(chat_id, p.get("label", ""))
    cmd, env = ai_command(c, prompt, p, resume=resume), job_env(p)
    job = {"id": job_id, "title": title[:120], "prompt": prompt[:4000],
           "category": category or guess_category(title),
           "status": "queued", "created": now_iso(), "output": "", "hint": "",
           "artifacts": [], "chat_id": chat_id or "", "resumed": bool(resume),
           "note": note, "session_id": "", "cost_usd": 0,
           "model": (p.get("model") if is_third_party(p)
                     else (c.get("ai", {}).get("model") or "")) or "默认",
           "provider": p.get("label", "")}
    with JOBS_LOCK:
        JOBS[job_id] = job
    t = threading.Thread(target=run_job, args=(job_id, cmd, env), daemon=True)
    t.start()
    return job


def load_job_history(limit=500):
    if not JOBS_LOG.exists():
        return
    try:
        lines = JOBS_LOG.read_text(encoding="utf-8").splitlines()[-limit:]
        for line in lines:
            try:
                rec = json.loads(line)
                rec.setdefault("category", guess_category(rec.get("title", "")))
                rec.setdefault("artifacts", extract_artifacts(rec.get("output", "")))
                JOBS[rec.get("id", uuid.uuid4().hex[:12])] = rec
            except json.JSONDecodeError:
                continue
    except OSError:
        pass


# ---------------- 看板统计(后端统一口径) ----------------

COLLAB_PEER = {"sales": "销售", "rd": "产研", "delivery": "交付"}


def compute_dashboard(c, customers, projects, archived, running_jobs,
                      collab=None, todos=None, meetings=None, mtodos=None):
    collab = collab if collab is not None else []
    todos = todos if todos is not None else []
    meetings = meetings if meetings is not None else []
    mtodos = mtodos if mtodos is not None else []
    def wan(v):
        try:
            return float(v)
        except (TypeError, ValueError):
            return 0.0

    today = datetime.now()
    week_ago = today - timedelta(days=7)
    meets_week, last_meet = 0, {}
    for mt in meetings:
        if not mt["date"]:
            continue
        try:
            dt = datetime.strptime(mt["date"], "%Y-%m-%d")
        except ValueError:
            continue
        if dt >= week_ago:
            meets_week += 1
        cid = mt.get("customer") or ""
        if cid and (last_meet.get(cid) is None or dt > last_meet[cid]):
            last_meet[cid] = dt

    active = [p for p in projects if p["meta"].get("stage") not in ("赢单", "丢单")]
    pipeline_budget = sum(wan(p["meta"].get("budget_wan")) for p in active)
    total_contract = sum(wan(p["meta"].get("contract_wan")) for p in projects) + \
        sum(wan(a["meta"].get("contract_wan")) for a in archived)

    dist = []
    for st in [s for s in c.get("project_stages", []) if s not in ("赢单", "丢单")]:
        ps = [p for p in projects if p["meta"].get("stage") == st]
        dist.append({"stage": st, "count": len(ps),
                     "budget_wan": round(sum(wan(p["meta"].get("budget_wan")) for p in ps), 2)})

    risks, seen = [], set()

    def add(kind, rid, msg):
        key = (kind, rid, msg)
        if key not in seen:
            seen.add(key)
            risks.append({"kind": kind, "id": rid, "msg": msg})

    for p in projects:
        st = p["meta"].get("stage", "")
        cust = p["meta"].get("customer", "")
        has = lambda slot: any(s["slot"] == slot and s["files"] for s in p["slots"])  # noqa: E731
        if st in ("商务", "投标", "签约") and not has("04-margin"):
            add("project", p["dir"], f"处于「{st}」阶段但缺毛利测算")
        if st == "签约" and not has("07-contract-review"):
            add("project", p["dir"], "即将签约但缺合同评审")
        if st == "赢单" and not wan(p["meta"].get("contract_wan")):
            add("project", p["dir"], "已赢单但未登记合同金额")
        if p["done"] == 0 and st not in ("赢单", "丢单"):
            add("project", p["dir"], "尚无任何交付物")
        if st not in ("赢单", "丢单") and cust:
            lm = last_meet.get(cust)
            if lm is None:
                add("customer", cust, "无任何拜访纪要")
            elif lm < today - timedelta(days=14):
                add("customer", cust, f"已 {(today - lm).days} 天未跟进")

    # 协作纳入风险:受阻 / 过期未完成
    today_s = today.strftime("%Y-%m-%d")
    for it in collab:
        peer = COLLAB_PEER.get(it.get("track"), "协作方")
        st = it.get("status", "")
        if st == "受阻":
            add("project", it.get("project", ""),
                f"与{peer}的协作「{it.get('type', '')}」受阻")
        elif st != "已完成" and it.get("due") and it["due"] < today_s:
            add("project", it.get("project", ""),
                f"与{peer}的协作「{it.get('type', '')}」已过期未完成")

    # To Do 合成:未完成协作动作(自动)+ 自定义任务
    todo_list = []
    for it in collab:
        if it.get("status") in ("待启动", "进行中", "受阻"):
            todo_list.append({"kind": "collab", "id": it.get("id"),
                              "peer": COLLAB_PEER.get(it.get("track"), ""),
                              "type": it.get("type", ""), "title": it.get("title", ""),
                              "project": it.get("project", ""),
                              "due": it.get("due", ""), "status": it.get("status", "")})
    for t in todos:
        if not t.get("done"):
            todo_list.append({"kind": "custom", "id": t.get("id"),
                              "title": t.get("title", ""), "due": t.get("due", ""),
                              "note": t.get("note", ""), "status": "进行中"})
    for t in mtodos:      # 纪要里的行动项自动流入
        if not t.get("done"):
            todo_list.append({"kind": "meeting", "id": t["id"], "path": t["path"],
                              "task_index": t["task_index"], "title": t["title"],
                              "owner": t.get("owner", ""), "due": t.get("due", ""),
                              "project": t.get("project", ""),
                              "customer": t.get("customer", ""),
                              "topic": t.get("topic", ""),
                              "meeting_date": t.get("meeting_date", ""),
                              "status": "进行中"})

    # 已完成历史(协作已完成 + 自定义已勾选,按完成时间倒序)
    done_list = []
    for it in collab:
        if it.get("status") == "已完成":
            done_list.append({"kind": "collab", "id": it.get("id"),
                              "peer": COLLAB_PEER.get(it.get("track"), ""),
                              "type": it.get("type", ""), "title": it.get("title", ""),
                              "project": it.get("project", ""),
                              "done_at": it.get("done_at") or it.get("updated") or ""})
    for t in todos:
        if t.get("done"):
            done_list.append({"kind": "custom", "id": t.get("id"),
                              "title": t.get("title", ""), "due": t.get("due", ""),
                              "done_at": t.get("done_at") or ""})
    for t in mtodos:
        if t.get("done"):
            done_list.append({"kind": "meeting", "id": t["id"], "path": t["path"],
                              "task_index": t["task_index"], "title": t["title"],
                              "topic": t.get("topic", ""), "done_at": ""})
    done_list.sort(key=lambda x: x.get("done_at") or "", reverse=True)

    def todo_key(t):
        if t.get("status") == "受阻":
            w = 0
        elif t.get("due") and t["due"] < today_s:
            w = 1
        elif t.get("due") == today_s:
            w = 2
        else:
            w = 3
        return (w, t.get("due") or "9999-99-99")

    todo_list.sort(key=todo_key)

    # 协作三条线概览
    collab_summary = []
    for tr in ("sales", "rd", "delivery"):
        xs = [x for x in collab if x.get("track") == tr]
        collab_summary.append({
            "track": tr, "peer": COLLAB_PEER[tr],
            "open": sum(1 for x in xs if x.get("status") in ("待启动", "进行中")),
            "stuck": sum(1 for x in xs if x.get("status") == "受阻"),
            "done": sum(1 for x in xs if x.get("status") == "已完成")})

    return {"stats": {"customers": len(customers), "active_projects": len(active),
                      "pipeline_budget_wan": round(pipeline_budget, 2),
                      "total_contract_wan": round(total_contract, 2),
                      "meetings_this_week": meets_week, "running_jobs": running_jobs,
                      "todo_open": len(todo_list), "todo_done": len(done_list)},
            "stage_dist": dist, "risks": risks[:10],
            "todos": todo_list[:30], "todos_done": done_list[:80],
            "collab_summary": collab_summary,
            "computed_at": now_iso()}


# ---------------- HTTP ----------------

class Handler(BaseHTTPRequestHandler):
    server_version = "PresalesWorkbench/1.0"

    # --- helpers
    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        data = body if isinstance(body, bytes) else \
            json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _json(self):
        n = int(self.headers.get("Content-Length") or 0)
        if n <= 0:
            return {}
        return json.loads(self.rfile.read(n).decode("utf-8"))

    def _err(self, e):
        code = 403 if isinstance(e, PermissionError) else \
            404 if isinstance(e, FileNotFoundError) else 400
        self._send(code, {"error": str(e)})

    def log_message(self, fmt, *args):  # 安静一点
        if "/api/jobs" not in (args[0] if args else ""):
            sys.stderr.write("[http] " + fmt % args + "\n")

    # --- GET
    def do_GET(self):
        u = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        try:
            if u.path in ("/", "/index.html"):
                self._send(200, (BIN / "app.html").read_bytes(),
                           "text/html; charset=utf-8")
            elif u.path == "/raw":
                p = safe_path(unquote(q.get("path", "")), must_exist=True)
                parts = p.relative_to(ROOT).parts
                if not parts or parts[0] not in DATA_DIRS:
                    raise PermissionError("仅限数据区文件")
                ctype = RAW_TYPES.get(p.suffix.lower())
                if not ctype:
                    raise PermissionError(f"不支持直接预览 {p.suffix}")
                self._send(200, p.read_bytes(), ctype)
            elif u.path == "/api/state":
                self._send(200, self.state())
            elif u.path == "/api/file":
                p = safe_path(unquote(q.get("path", "")), must_exist=True)
                if p.suffix.lower() not in EDIT_SUFFIX:
                    raise PermissionError("该类型请用「本地打开」")
                self._send(200, {"path": q.get("path"), "content": read_text(p)})
            elif u.path == "/print":
                self._send(200, print_page(unquote(q.get("path", ""))).encode("utf-8"),
                           "text/html; charset=utf-8")
            elif u.path == "/download":
                p = safe_path(unquote(q.get("path", "")), must_exist=True)
                parts = p.relative_to(ROOT).parts
                if not parts or parts[0] not in DATA_DIRS:
                    raise PermissionError("仅限数据区文件")
                import mimetypes
                ctype = mimetypes.guess_type(p.name)[0] or "application/octet-stream"
                data = p.read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", ctype)
                from urllib.parse import quote as _q
                self.send_header("Content-Disposition",
                                 f"attachment; filename*=UTF-8''{_q(p.name)}")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
            elif u.path == "/api/meeting-stream":
                # 项目纪要瀑布流:最新在前,一次返回全部正文
                proj = unquote(q.get("project", ""))
                items = [m for m in scan_meetings() if m["project"] == proj]
                for m in items:
                    try:
                        m["content"] = read_text(ROOT / m["path"], limit=200_000)
                    except OSError:
                        m["content"] = "(读取失败)"
                self._send(200, {"project": proj, "items": items})
            elif u.path == "/api/trash":
                self._send(200, {"items": load_trash()})
            elif u.path == "/api/templates":
                self._send(200, {"slots": scan_templates(cfg())})
            elif u.path == "/api/market":
                self._send(200, parse_market(cfg(), force=bool(q.get("force"))))
            elif u.path == "/api/search":
                kw = q.get("q", "").strip()
                self._send(200, {"q": kw, "hits": search_all(kw) if len(kw) >= 2 else []})
            elif u.path == "/api/chat":
                cid = (q.get("id") or "").strip()
                self._send(200, {"chat_id": cid, "turns": chat_jobs(cid) if cid else [],
                                 "chats": chat_list()})
            elif u.path == "/api/jobs":
                kw = (q.get("q") or "").strip().lower()
                cat = q.get("cat") or ""
                st = q.get("status") or ""
                since = q.get("since") or ""      # YYYY-MM-DD
                limit = min(int(q.get("limit") or 60), 300)
                want_arch = q.get("archived") == "1"
                with JOBS_LOCK:
                    everyj = sorted(JOBS.values(), key=lambda j: j.get("created", ""),
                                    reverse=True)
                n_arch = sum(1 for j in everyj if j.get("archived"))
                # 归档任务默认不进列表(但仍留在它所属的对话里)
                allj = [j for j in everyj if bool(j.get("archived")) == want_arch]
                counts = {}
                for j in allj:
                    counts[j.get("category", "other")] = \
                        counts.get(j.get("category", "other"), 0) + 1
                sel = []
                for j in allj:
                    if cat and j.get("category") != cat:
                        continue
                    if st and j.get("status") != st:
                        continue
                    if since and (j.get("created", "")[:10] < since):
                        continue
                    if kw and kw not in (j.get("title", "") + j.get("output", "") +
                                         j.get("prompt", "")).lower():
                        continue
                    sel.append(j)
                self._send(200, {"jobs": sel[:limit], "total": len(allj),
                                 "matched": len(sel), "counts": counts,
                                 "archived_total": n_arch, "archived_view": want_arch,
                                 "categories": JOB_CATEGORIES})
            else:
                self._send(404, {"error": "not found"})
        except Exception as e:  # noqa: BLE001
            self._err(e)

    # --- POST
    def do_POST(self):
        u = urlparse(self.path)
        try:
            body = self._json()
            c = cfg()
            if u.path == "/api/customer":
                self._send(200, self.create_customer(c, body))
            elif u.path == "/api/project":
                self._send(200, self.create_project(c, body))
            elif u.path == "/api/meeting":
                self._send(200, self.create_meeting(body))
            elif u.path == "/api/file":
                p = writable_path(body.get("path", ""))
                if not p.parent.is_dir():
                    raise FileNotFoundError(f"目录不存在: {p.parent}")
                p.write_text(body.get("content", ""), encoding="utf-8")
                rel = str(p.relative_to(ROOT))
                if rel.startswith("plugins/skills/") and p.name == "SKILL.md":
                    mirror_skill(p.parent.name)   # 技能改动即时生效到 .claude/skills/
                self._send(200, {"ok": True, "path": body.get("path")})
            elif u.path == "/api/skill":
                name = (body.get("name") or "").strip()
                if not SKILL_NAME_RE.match(name):
                    raise ValueError("技能标识须为小写英文/数字/短横线,如 bid-response-check")
                desc = (body.get("description") or "").strip().replace("\n", " ")
                content = body.get("body") or ""
                d = ROOT / "plugins" / "skills" / name
                d.mkdir(parents=True, exist_ok=True)
                (d / "SKILL.md").write_text(
                    f"---\nname: {name}\ndescription: {desc}\n---\n\n{content}\n",
                    encoding="utf-8")
                mirror_skill(name)
                self._send(200, {"ok": True, "name": name})
            elif u.path == "/api/delete":
                # 统一删除:文件/目录移入回收站(客户/项目/纪要/场景/知识文件…)
                rel = body.get("path", "")
                kind = body.get("kind", "file")
                tid = move_to_trash(rel, kind, body.get("note", ""))
                if kind == "scenario":
                    ind = Path(rel).parent
                    d = ROOT / ind
                    if d.is_dir() and not any(d.iterdir()):
                        d.rmdir()                 # 行业目录空了就一并清掉
                self._send(200, {"ok": True, "trash_id": tid})
            elif u.path == "/api/trash-restore":
                self._send(200, {"ok": True, "path": restore_from_trash(body.get("id", ""))})
            elif u.path == "/api/trash-purge":
                with TRASH_LOCK:
                    items = load_trash()
                    tid = body.get("id")
                    if tid:
                        shutil.rmtree(TRASH_DIR / tid, ignore_errors=True)
                        items = [x for x in items if x["id"] != tid]
                    else:
                        for x in items:
                            shutil.rmtree(TRASH_DIR / x["id"], ignore_errors=True)
                        items = []
                    save_trash(items)
                self._send(200, {"ok": True, "left": len(items)})
            elif u.path == "/api/template-upload":
                import base64
                slot = body.get("slot", "")
                if not any(s["slot"] == slot for s in c.get("pipeline", [])) \
                        and slot != "customer-profile":
                    raise ValueError(f"未知槽位: {slot}")
                fname = slug(body.get("filename", "template"))
                suf = Path(fname).suffix.lower()
                if suf in BLOCKED_UPLOAD:
                    raise PermissionError(f"出于安全考虑不接受 {suf} 文件")
                data = base64.b64decode(body.get("content_b64", ""))
                if len(data) > 30 * 1024 * 1024:
                    raise ValueError("文件超过 30MB")
                name = (body.get("name") or Path(fname).stem).strip()[:60]
                tags = [t.strip()[:16] for t in (body.get("tags") or [])
                        if t and t.strip()][:5]
                with TPL_LOCK:
                    tpls, defaults = load_user_tpl()
                    tid = uuid.uuid4().hex[:8]
                    sdir = USER_TPL_DIR / slot
                    sdir.mkdir(parents=True, exist_ok=True)
                    tmp = sdir / f".raw-{tid}{suf}"
                    tmp.write_bytes(data)
                    try:
                        md, outsuf = convert_to_md(
                            tmp, name, origin_name=fname,
                            keep_original=bool(body.get("keep_original")))
                        if md is None:                       # xlsx 等保留原格式
                            final = sdir / f"{tid}{suf}"
                            shutil.move(str(tmp), str(final))
                        else:
                            final = sdir / f"{tid}.md"
                            final.write_text(md, encoding="utf-8")
                            tmp.unlink(missing_ok=True)
                    except Exception:
                        tmp.unlink(missing_ok=True)
                        raise
                    tpls.append({"id": tid, "slot": slot, "name": name, "tags": tags,
                                 "file": str(final.relative_to(USER_TPL_DIR)),
                                 "origin_name": fname, "source": "upload",
                                 "created": now_iso()})
                    if body.get("set_default"):
                        defaults[slot] = tid
                    save_user_tpl(tpls, defaults)
                self._send(200, {"ok": True, "id": tid,
                                 "path": str(final.relative_to(ROOT)),
                                 "converted": final.suffix.lower() != suf})
            elif u.path == "/api/template-meta":
                op = body.get("op")
                tid = body.get("id", "")
                with TPL_LOCK:
                    tpls, defaults = load_user_tpl()
                    if op == "default":
                        slot = body.get("slot", "")
                        defaults[slot] = tid
                    elif op == "update":
                        for t in tpls:
                            if t["id"] == tid:
                                if body.get("name"):
                                    t["name"] = body["name"].strip()[:60]
                                if body.get("tags") is not None:
                                    t["tags"] = [x.strip()[:16] for x in body["tags"]
                                                 if x and x.strip()][:5]
                    elif op == "delete":
                        hit = next((t for t in tpls if t["id"] == tid), None)
                        if not hit:
                            raise FileNotFoundError("模板不存在(系统默认模板不可删除)")
                        (USER_TPL_DIR / hit["file"]).unlink(missing_ok=True)
                        tpls = [t for t in tpls if t["id"] != tid]
                        defaults = {k: v for k, v in defaults.items() if v != tid}
                    else:
                        raise ValueError("op 须为 default/update/delete")
                    save_user_tpl(tpls, defaults)
                self._send(200, {"ok": True})
            elif u.path == "/api/skill-import":
                import base64
                import io
                import zipfile
                name = (body.get("name") or "").strip()
                if not SKILL_NAME_RE.match(name):
                    raise ValueError("技能标识须为小写英文/数字/短横线,如 my-skill")
                d = ROOT / "plugins" / "skills" / name
                if d.exists():
                    raise ValueError(f"技能已存在: {name}(先删除或换个标识)")
                d.mkdir(parents=True)
                try:
                    zb = body.get("zip_b64")
                    files = body.get("files") or []
                    if zb:
                        data = base64.b64decode(zb)
                        if len(data) > 30 * 1024 * 1024:
                            raise ValueError("zip 超过 30MB")
                        with zipfile.ZipFile(io.BytesIO(data)) as z:
                            names = [n for n in z.namelist() if not n.endswith("/")
                                     and "__MACOSX" not in n
                                     and not Path(n).name.startswith(".")]
                            cands = [n for n in names if Path(n).name == "SKILL.md"]
                            if not cands:
                                raise ValueError("zip 中未找到 SKILL.md")
                            root = Path(sorted(cands,
                                               key=lambda n: len(Path(n).parts))[0]).parent
                            for n in names:
                                rel = Path(n)
                                if str(root) not in ("", "."):
                                    try:
                                        rel = rel.relative_to(root)
                                    except ValueError:
                                        continue
                                if not rel.parts or ".." in rel.parts:
                                    continue
                                tgt = d / rel
                                tgt.parent.mkdir(parents=True, exist_ok=True)
                                tgt.write_bytes(z.read(n))
                    elif files:
                        # 文件夹/多文件导入:所有文件共享同一顶层目录时剥掉它
                        firsts = {Path(f.get("path") or "").parts[0]
                                  for f in files if Path(f.get("path") or "").parts}
                        strip = len(firsts) == 1 and \
                            all(len(Path(f.get("path") or "").parts) > 1 for f in files)
                        for f in files[:300]:
                            parts = [p for p in Path(f.get("path") or "").parts
                                     if p not in ("", ".", "..")]
                            if strip:
                                parts = parts[1:]
                            if not parts or Path(parts[-1]).name.startswith("."):
                                continue
                            data = base64.b64decode(f.get("content_b64", ""))
                            if len(data) > 10 * 1024 * 1024:
                                continue
                            tgt = d / Path(*parts)
                            tgt.parent.mkdir(parents=True, exist_ok=True)
                            tgt.write_bytes(data)
                        if not (d / "SKILL.md").exists():
                            mds = sorted(d.rglob("*.md"),
                                         key=lambda p: len(p.relative_to(d).parts))
                            if len(mds) == 1:
                                mds[0].rename(d / "SKILL.md")
                    else:
                        raise ValueError("未提供 zip 或文件")
                    if not (d / "SKILL.md").exists():
                        raise ValueError("未找到 SKILL.md(单个 .md 文件会自动转为 SKILL.md)")
                    mirror_skill(name)
                except Exception:
                    shutil.rmtree(d, ignore_errors=True)
                    raise
                self._send(200, {"ok": True, "name": name})
            elif u.path == "/api/market-import":
                ids = body.get("ids") or []
                if not ids:
                    raise ValueError("未选择技能")
                mk = parse_market(c)
                if mk.get("error"):
                    raise RuntimeError(mk["error"])
                by_id = {s["id"]: s for s in mk["skills"]}
                done, failed = [], []
                for sid in ids[:20]:
                    item = by_id.get(sid)
                    if not item:
                        failed.append(f"{sid}: 榜单中未找到")
                        continue
                    name = sid if SKILL_NAME_RE.match(sid) else \
                        re.sub(r"[^a-z0-9-]", "-", sid.lower())[:40].strip("-")
                    if not SKILL_NAME_RE.match(name):
                        failed.append(f"{sid}: 标识非法")
                        continue
                    d = ROOT / "plugins" / "skills" / name
                    if d.exists():
                        failed.append(f"{name}: 已存在,跳过")
                        continue
                    fetched, src_url = fetch_skill_md(item["url"])
                    d.mkdir(parents=True, exist_ok=True)
                    (d / "SKILL.md").write_text(market_skill_md(item, fetched, src_url),
                                                encoding="utf-8")
                    mirror_skill(name)
                    done.append({"name": name, "full": bool(fetched)})
                self._send(200, {"ok": True, "imported": done, "failed": failed})
            elif u.path == "/api/skill-delete":
                name = (body.get("name") or "").strip()
                if not SKILL_NAME_RE.match(name):
                    raise ValueError("非法技能标识")
                shutil.rmtree(ROOT / "plugins" / "skills" / name, ignore_errors=True)
                shutil.rmtree(ROOT / ".claude" / "skills" / name, ignore_errors=True)
                self._send(200, {"ok": True})
            elif u.path == "/api/roles":
                roles = body.get("roles")
                if not isinstance(roles, list):
                    raise ValueError("roles 须为数组")
                save_roles(roles)
                self._send(200, {"ok": True})
            elif u.path == "/api/upload":
                import base64
                rel_dir = body.get("dir", "inbox")
                fname = slug(body.get("filename", "upload"))
                if not fname:
                    raise ValueError("文件名为空")
                d = safe_path(rel_dir)
                if d.relative_to(ROOT).parts[0] not in DATA_DIRS:
                    raise PermissionError("仅可上传到数据区目录")
                suffix = Path(fname).suffix.lower()
                if suffix not in UPLOAD_SUFFIX:
                    raise PermissionError(f"不支持的文件类型 {suffix}")
                data = base64.b64decode(body.get("content_b64", ""))
                if len(data) > 60 * 1024 * 1024:
                    raise ValueError("文件超过 60MB")
                d.mkdir(parents=True, exist_ok=True)
                p = d / fname
                if not body.get("overwrite"):
                    i = 2
                    while p.exists():
                        p = d / f"{Path(fname).stem}-{i}{suffix}"
                        i += 1
                p.write_bytes(data)
                self._send(200, {"ok": True, "path": str(p.relative_to(ROOT))})
            elif u.path == "/api/export":
                out = export_doc(body.get("path", ""), body.get("format", "docx"))
                self._send(200, {"ok": True, "path": out})
            elif u.path == "/api/email":
                to = [x.strip() for x in (body.get("to") or "").replace(";", ",").split(",")
                      if x.strip()]
                if not to:
                    raise ValueError("收件人不能为空")
                send_mail(to, body.get("subject") or "售前工作台文档",
                          body.get("body") or "", body.get("attach") or [])
                self._send(200, {"ok": True})
            elif u.path == "/api/email-config":
                conf = {k: body.get(k, "") for k in
                        ("smtp_host", "smtp_port", "smtp_user", "smtp_pass", "from")}
                conf["use_ssl"] = bool(body.get("use_ssl", True))
                JOBS_DIR.mkdir(exist_ok=True)
                (ROOT / ".workbench" / "email.json").write_text(
                    json.dumps(conf, ensure_ascii=False, indent=2), encoding="utf-8")
                self._send(200, {"ok": True})
            elif u.path == "/api/event":
                op = body.get("op")
                ev = body.get("event") or {}
                with CAL_LOCK:
                    events = load_events()
                    if op == "add":
                        ev["id"] = uuid.uuid4().hex[:8]
                        ev["notified"] = False
                        events.append(ev)
                    elif op == "update":
                        events = [dict(e, **ev, notified=e.get("notified", False)
                                       if e.get("date") == ev.get("date") and
                                       e.get("time") == ev.get("time") else False)
                                  if e.get("id") == ev.get("id") else e for e in events]
                    elif op == "delete":
                        events = [e for e in events if e.get("id") != ev.get("id")]
                    else:
                        raise ValueError("op 须为 add/update/delete")
                    save_events(events)
                self._send(200, {"ok": True})
            elif u.path == "/api/meeting-todo":
                self._send(200, {"ok": True, "path": toggle_meeting_todo(
                    body.get("path", ""), int(body.get("index", -1)),
                    bool(body.get("checked")))})
            elif u.path == "/api/todo":
                op = body.get("op")
                t = body.get("todo") or {}
                with TODO_LOCK:
                    items = load_todos()
                    if op == "add":
                        if not (t.get("title") or "").strip():
                            raise ValueError("待办内容不能为空")
                        t["id"] = uuid.uuid4().hex[:8]
                        t["done"] = False
                        t["created"] = now_iso()
                        items.append(t)
                    elif op == "update":
                        out = []
                        for x in items:
                            if x.get("id") == t.get("id"):
                                m = dict(x, **t)
                                if m.get("done"):
                                    m.setdefault("done_at", now_iso())
                                else:
                                    m.pop("done_at", None)
                                out.append(m)
                            else:
                                out.append(x)
                        items = out
                    elif op == "delete":
                        items = [x for x in items if x.get("id") != t.get("id")]
                    else:
                        raise ValueError("op 须为 add/update/delete")
                    save_todos(items)
                self._send(200, {"ok": True})
            elif u.path == "/api/meeting-bind":
                # 把历史遗留的客户级纪要归属到某个项目(移动文件)
                src = safe_path(body.get("path", ""), must_exist=True)
                if src.suffix.lower() != ".md" or "meetings" not in src.parts:
                    raise ValueError("只能归属纪要文件")
                project = slug(body.get("project", ""))
                pdir = ROOT / "projects" / project
                if not pdir.is_dir():
                    pdir = ROOT / "archive" / project     # 历史纪要常属于已归档项目
                if not pdir.is_dir():
                    raise FileNotFoundError(f"项目不存在: {project}")
                dst_dir = pdir / "meetings"
                dst_dir.mkdir(exist_ok=True)
                dst, i = dst_dir / src.name, 2
                while dst.exists():
                    dst = dst_dir / f"{src.stem}-{i}{src.suffix}"
                    i += 1
                shutil.move(str(src), str(dst))
                self._send(200, {"ok": True, "path": str(dst.relative_to(ROOT))})
            elif u.path == "/api/collab":
                op = body.get("op")
                it = body.get("item") or {}
                with COLLAB_LOCK:
                    items = load_collab()
                    if op == "add":
                        if not (it.get("project") or "").strip():
                            raise ValueError("每一个协作动作必须绑定一个项目")
                        it["id"] = uuid.uuid4().hex[:8]
                        it["created"] = now_iso()
                        items.append(it)
                    elif op == "update":
                        # 支持部分更新(如只改 status):校验合并后的结果,而非传入片段
                        cur = next((x for x in items if x.get("id") == it.get("id")), None)
                        if cur is None:
                            raise FileNotFoundError("协作动作不存在")
                        merged = dict(cur, **it)
                        if not (merged.get("project") or "").strip():
                            raise ValueError("每一个协作动作必须绑定一个项目")
                        merged["updated"] = now_iso()
                        if merged.get("status") == "已完成":
                            merged.setdefault("done_at", now_iso())
                        else:
                            merged.pop("done_at", None)
                        items = [merged if x.get("id") == it.get("id") else x for x in items]
                    elif op == "delete":
                        items = [x for x in items if x.get("id") != it.get("id")]
                    else:
                        raise ValueError("op 须为 add/update/delete")
                    save_collab(items)
                self._send(200, {"ok": True})
            elif u.path == "/api/schedule":
                op = body.get("op")
                it = body.get("schedule") or {}
                with SCHED_LOCK:
                    items = load_schedules()
                    if op == "add":
                        it["id"] = uuid.uuid4().hex[:8]
                        items.append(it)
                    elif op == "update":
                        items = [it if x.get("id") == it.get("id") else x for x in items]
                    elif op == "delete":
                        items = [x for x in items if x.get("id") != it.get("id")]
                    else:
                        raise ValueError("op 须为 add/update/delete")
                    save_schedules(items)
                self._send(200, {"ok": True, "schedules": items})
            elif u.path == "/api/ai-permission":
                mode = body.get("mode", "")
                if mode not in PERMISSION_MODES:
                    raise ValueError(f"权限模式须为 {PERMISSION_MODES} 之一")
                cpath = ROOT / "workbench.json"
                conf = wb.load_json(cpath) or {}
                conf.setdefault("ai", {})["permission_mode"] = mode
                cpath.write_text(json.dumps(conf, ensure_ascii=False, indent=2) + "\n",
                                 encoding="utf-8")
                self._send(200, {"ok": True, "mode": mode})
            elif u.path == "/api/ai-model":
                model = (body.get("model") or "").strip()
                if model and not re.fullmatch(r"[A-Za-z0-9._\-]{1,64}", model):
                    raise ValueError("模型名只允许字母数字与 . _ -")
                cpath = ROOT / "workbench.json"
                conf = wb.load_json(cpath) or {}
                conf.setdefault("ai", {})["model"] = model
                cpath.write_text(json.dumps(conf, ensure_ascii=False, indent=2) + "\n",
                                 encoding="utf-8")
                self._send(200, {"ok": True, "model": model})
            elif u.path == "/api/provider":
                # 保存/切换供应商。token 为空字符串=不改动(前端只拿得到脱敏值),
                # 传 "__clear__" 才是真的清空。
                d = load_providers()
                pid = (body.get("id") or "").strip()
                known = {p["id"] for p in PROVIDER_PRESETS}
                if pid:
                    if pid not in known:
                        raise ValueError(f"未知供应商:{pid}")
                    it = dict((d.get("items") or {}).get(pid) or {})
                    for k in ("base_url", "model", "small_model"):
                        if k in body:
                            it[k] = (body.get(k) or "").strip()
                    if it.get("base_url") and not it["base_url"].startswith(("http://", "https://")):
                        raise ValueError("BASE_URL 需以 http:// 或 https:// 开头")
                    tok = body.get("token")
                    if tok == "__clear__":
                        it.pop("token", None)
                    elif tok:
                        it["token"] = tok.strip()
                    d.setdefault("items", {})[pid] = it
                act = (body.get("active") or "").strip()
                if act:
                    if act not in known:
                        raise ValueError(f"未知供应商:{act}")
                    chk = provider_by_id(act, d)
                    if act != "anthropic" and not (chk.get("base_url") or "").strip():
                        raise ValueError("该供应商还没配 BASE_URL,先填好再切换")
                    if chk.get("needs_key") and not (chk.get("token") or "").strip():
                        raise ValueError("该供应商还没填 API key,先填好再切换")
                    d["active"] = act
                save_providers(d)
                self._send(200, {"ok": True, "providers": providers_public()})
            elif u.path == "/api/provider-test":
                # 连通性实测:发一句最短的话,不碰工具,只验端点+key+模型名
                if not claude_available():
                    raise FileNotFoundError("本机未找到 claude CLI")
                pid = (body.get("id") or "").strip()
                p = provider_by_id(pid) if pid else active_provider()
                env, t0 = job_env(p), time.time()
                cmd = ["claude", "-p", "回复两个字符:OK"]
                mdl = "" if is_third_party(p) else (c.get("ai", {}).get("model") or "").strip()
                if mdl:
                    cmd += ["--model", mdl]
                try:
                    r = subprocess.run(cmd, capture_output=True, text=True, cwd=str(ROOT),
                                       env=env, timeout=120, stdin=subprocess.DEVNULL)
                except subprocess.TimeoutExpired:
                    raise RuntimeError("测试超时(120 秒):端点不通或响应过慢") from None
                strip = lambda s: re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "", s or "").strip()  # noqa: E731
                ms = int((time.time() - t0) * 1000)
                if r.returncode != 0:
                    msg = strip(r.stderr) or strip(r.stdout) or "无输出"
                    raise RuntimeError((auth_hint(msg, env) or msg)[:300])
                self._send(200, {"ok": True, "ms": ms, "reply": strip(r.stdout)[:200],
                                 "label": p.get("label", ""),
                                 "model": (p.get("model") if is_third_party(p) else mdl) or "CLI 默认"})
            elif u.path == "/api/open-claude":
                r = subprocess.run(["open", "-a", "Claude"], capture_output=True, text=True)
                if r.returncode != 0:
                    raise FileNotFoundError("未找到 Claude 桌面应用,请先安装 Claude Desktop/Code App")
                self._send(200, {"ok": True})
            elif u.path == "/api/transcribe":
                tconf = (cfg().get("transcribe") or {}).get("command")
                if not tconf:
                    raise RuntimeError(
                        "未配置转写引擎:在 workbench.json 的 transcribe.command 配置本地转写命令"
                        "(如 whisper.cpp / mlx-whisper),{audio} 会被替换为音频路径;"
                        "或改用会议软件自带转写导出文本后导入")
                p = safe_path(body.get("path", ""), must_exist=True)
                cmd = [str(p) if a == "{audio}" else a for a in tconf]
                r = subprocess.run(cmd, capture_output=True, text=True, timeout=1800)
                if r.returncode != 0:
                    raise RuntimeError(f"转写失败: {(r.stderr or '')[:200]}")
                self._send(200, {"ok": True, "text": (r.stdout or "").strip()[:200000]})
            elif u.path == "/api/extract-project":
                proj = slug(body.get("project", ""))
                base = ROOT / "projects" / proj
                if not base.is_dir():
                    base = ROOT / "archive" / proj
                if not base.is_dir():
                    raise FileNotFoundError(f"项目不存在: {proj}")
                made, skipped = extract_dir_sidecars(base)
                m2, s2 = (0, 0)
                if (base / "meetings").is_dir():
                    m2, s2 = extract_dir_sidecars(base / "meetings")
                self._send(200, {"ok": True, "made": made + m2, "skipped": skipped + s2})
            elif u.path == "/api/slot-upload":
                # 直接把已有文件放进某槽位:自动补槽位前缀,使其被识别为该槽位交付物
                import base64
                proj = slug(body.get("project", ""))
                slot = body.get("slot", "")
                d = ROOT / "projects" / proj
                if not d.is_dir():
                    raise FileNotFoundError(f"项目不存在: {proj}")
                pref = slot_prefix(slot)
                if not pref:
                    raise ValueError(f"槽位无编号前缀: {slot}")
                saved = []
                for f in (body.get("files") or [])[:20]:
                    fname = slug(f.get("filename", "file"))
                    if not fname:
                        continue
                    if Path(fname).suffix.lower() in BLOCKED_UPLOAD:
                        raise PermissionError(f"不接受 {Path(fname).suffix} 文件")
                    if not re.match(rf"^{pref}[-_]", fname):
                        fname = f"{pref}-{fname}"
                    data = base64.b64decode(f.get("content_b64", ""))
                    if len(data) > 60 * 1024 * 1024:
                        raise ValueError(f"{fname} 超过 60MB")
                    p, i = d / fname, 2
                    while p.exists():
                        p = d / f"{Path(fname).stem}-{i}{Path(fname).suffix}"
                        i += 1
                    p.write_bytes(data)
                    saved.append(str(p.relative_to(ROOT)))
                    extract_sidecar(p)      # 立刻生成可供 AI 读取的文本副本
                if not saved:
                    raise ValueError("没有可上传的文件")
                self._send(200, {"ok": True, "paths": saved})
            elif u.path == "/api/slot":
                self._send(200, self.instantiate_slot(c, body))
            elif u.path == "/api/ai":
                prompt = (body.get("prompt") or "").strip()
                if not prompt:
                    raise ValueError("prompt 不能为空")
                if not claude_available():
                    raise FileNotFoundError("本机未找到 claude CLI")
                cid = (body.get("chat_id") or "").strip()[:32]
                if cid and not re.fullmatch(r"[a-z0-9]{6,32}", cid):
                    raise ValueError("chat_id 非法")
                job = submit_job(c, body.get("title") or prompt[:60], prompt,
                                 chat_id=cid or None)
                self._send(200, {"job": job})
            elif u.path == "/api/chat-new":
                self._send(200, {"ok": True, "chat_id": uuid.uuid4().hex[:12]})
            elif u.path == "/api/job":
                op = body.get("op") or ""
                jid = (body.get("id") or "").strip()
                n = 0
                with JOBS_LOCK:
                    if op in ("archive", "unarchive"):
                        j = JOBS.get(jid)
                        if not j:
                            raise FileNotFoundError("任务不存在(可能已被删除)")
                        if j.get("status") in ("queued", "running"):
                            raise ValueError("任务还在运行,跑完再归档")
                        j["archived"] = (op == "archive")
                        n = 1
                    elif op == "delete":
                        j = JOBS.get(jid)
                        if not j:
                            raise FileNotFoundError("任务不存在")
                        if j.get("status") in ("queued", "running"):
                            raise ValueError("任务还在运行,不能删除")
                        del JOBS[jid]
                        n = 1
                    elif op == "delete-chat":
                        cid = (body.get("chat_id") or "").strip()
                        if not cid:
                            raise ValueError("缺少 chat_id")
                        tgt = [k for k, v in JOBS.items() if v.get("chat_id") == cid]
                        if any(JOBS[k].get("status") in ("queued", "running") for k in tgt):
                            raise ValueError("该对话还有任务在跑,跑完再删")
                        for k in tgt:
                            del JOBS[k]
                        n = len(tgt)
                    elif op == "clear-archived":
                        tgt = [k for k, v in JOBS.items() if v.get("archived")]
                        for k in tgt:
                            del JOBS[k]
                        n = len(tgt)
                    else:
                        raise ValueError("op 须为 archive/unarchive/delete/delete-chat/clear-archived")
                save_jobs_log()
                self._send(200, {"ok": True, "affected": n})
            elif u.path == "/api/ai-quick":
                # 同步小任务:生成一段文本直接返回(如定时任务指令改写),不进任务队列
                prompt = (body.get("prompt") or "").strip()
                if not prompt:
                    raise ValueError("prompt 不能为空")
                if not claude_available():
                    raise FileNotFoundError("本机未找到 claude CLI")
                p = active_provider()
                env = job_env(p)
                r = subprocess.run(ai_command(c, prompt, p), capture_output=True,
                                   text=True, cwd=str(ROOT), env=env, timeout=180,
                                   stdin=subprocess.DEVNULL)
                strip = lambda s: re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "", s or "").strip()  # noqa: E731
                if r.returncode != 0:
                    msg = strip(r.stderr) or strip(r.stdout) or "生成失败"
                    msg = (auth_hint(msg, env) or msg)
                    raise RuntimeError(msg[:300])
                self._send(200, {"ok": True, "text": strip(r.stdout)[:8000]})
            elif u.path == "/api/open":
                p = safe_path(body.get("path", ""), must_exist=True)
                if sys.platform == "darwin":
                    subprocess.Popen(["open", str(p)])
                    self._send(200, {"ok": True})
                else:
                    raise PermissionError("仅支持 macOS 的本地打开")
            elif u.path == "/api/weekly":
                self._send(200, {"ok": True, "path": make_weekly(c)})
            elif u.path == "/api/archive-project":
                proj = slug(body.get("project", ""))
                src = ROOT / "projects" / proj
                if not src.is_dir():
                    raise FileNotFoundError(f"项目不存在: {proj}")
                dst = ROOT / "archive" / proj
                i = 2
                while dst.exists():
                    dst = ROOT / "archive" / f"{proj}-{i}"
                    i += 1
                shutil.move(str(src), str(dst))
                # 归档摘要:项目信息 + 文件清单快照
                meta = wb.load_json(dst / "project.json") or {}
                files = sorted(f.name for f in dst.iterdir()
                               if f.is_file() and not f.name.startswith("."))
                summary = [f"# 归档摘要 · {meta.get('name', proj)}", "",
                           f"- 客户:{meta.get('customer', '')}",
                           f"- 最终阶段:{meta.get('stage', '')}",
                           f"- 预算:{meta.get('budget_wan', '—')} 万",
                           f"- 合同额:{meta.get('contract_wan', '—')} 万",
                           f"- 签约日期:{meta.get('sign_date', '—')}",
                           f"- 创建:{meta.get('created', '')}",
                           f"- 归档时间:{now_iso()}", "", "## 归档文件清单", ""]
                summary += [f"- {n}" for n in files]
                (dst / "归档摘要.md").write_text("\n".join(summary) + "\n", encoding="utf-8")
                self._send(200, {"ok": True, "dir": dst.name})
            elif u.path == "/api/restore-project":
                proj = slug(body.get("project", ""))
                src = ROOT / "archive" / proj
                if not src.is_dir():
                    raise FileNotFoundError(f"归档中不存在: {proj}")
                dst = ROOT / "projects" / proj
                if dst.exists():
                    raise ValueError(f"projects/ 下已有同名目录,无法恢复: {proj}")
                shutil.move(str(src), str(dst))
                # 清掉归档摘要:项目已回到在营状态,留着会造成"已归档"的假象
                # (下次归档时会重新生成)
                (dst / "归档摘要.md").unlink(missing_ok=True)
                self._send(200, {"ok": True, "dir": proj})
            elif u.path == "/api/extract":
                # 文档提取:PDF/DOCX/PPTX/XLSX → knowledge/.extracted/*.md(可检索)
                cmd = [sys.executable, str(BIN / "extract.py")]
                if body.get("all"):
                    cmd.append("--all")
                job_id = uuid.uuid4().hex[:12]
                job = {"id": job_id, "title": "提取知识库文档" + ("(全量)" if body.get("all") else "(增量)"),
                       "prompt": " ".join(cmd), "status": "queued", "created": now_iso(),
                       "output": "", "hint": ""}
                with JOBS_LOCK:
                    JOBS[job_id] = job
                threading.Thread(target=run_job, args=(job_id, cmd), daemon=True).start()
                self._send(200, {"job": job})
            elif u.path == "/api/sync":
                state = wb.collect(c)
                wb.write_mcp(state)
                wb.copy_skills(state)
                self._send(200, {"ok": True})
            else:
                self._send(404, {"error": "not found"})
        except Exception as e:  # noqa: BLE001
            self._err(e)

    # --- 业务
    def state(self):
        c = cfg()
        pid, pdir, manifest = active_pack(c)
        files = manifest.get("files", {})
        pack_slots = {}
        for s in c.get("pipeline", []):
            rel = files.get(s["slot"])
            ok = bool(rel) and (pdir / rel).exists()
            pack_slots[s["slot"]] = {"mounted": ok,
                                     "file": Path(rel).name if rel else None,
                                     "tpl_path": f"{c['templates']['dir']}/{pid}/{rel}" if rel else None}
        mcps = []
        mdir = ROOT / c.get("mcp", {}).get("dir", "plugins/mcp")
        for mid in c.get("mcp", {}).get("enabled", []):
            frag = wb.load_json(mdir / f"{mid}.json")
            if not frag:
                mcps.append({"id": mid, "status": "missing"})
                continue
            for name, server in frag.get("mcpServers", {}).items():
                url = server.get("url", "")
                st = ("live" if wb.probe_http(url) else "offline") if url else "stdio"
                mcps.append({"id": name, "status": st, "url": url})
        with JOBS_LOCK:
            running = sum(1 for j in JOBS.values() if j.get("status") in ("queued", "running"))
        customers = scan_customers()
        projects = scan_projects(c)
        archived = scan_archived()
        meetings = scan_meetings()
        mtodos = scan_meeting_todos(meetings)
        return {
            "dashboard": compute_dashboard(c, customers, projects, archived, running,
                                           collab=load_collab(), todos=load_todos(),
                                           meetings=meetings, mtodos=mtodos),
            "meetings": meetings,
            "config": {"name": c.get("name"), "owner": c.get("owner"),
                       "company": c.get("company", {}).get("name", ""),
                       "stages": c.get("project_stages",
                                       ["调研", "方案", "POC", "商务", "投标", "签约", "赢单", "丢单"]),
                       "pipeline": c.get("pipeline", [])},
            "pack": {"id": pid, "version": manifest.get("version", "?"),
                     "label": manifest.get("label", ""),
                     "origin": manifest.get("origin", ""), "slots": pack_slots},
            "mcp": mcps,
            "claude": {"available": claude_available(), "running_jobs": running,
                       "permission_mode": (c.get("ai", {}).get("permission_mode")
                                           or "bypassPermissions"),
                       "permission_modes": PERMISSION_MODES,
                       "model": (c.get("ai", {}).get("model") or ""),
                       "models": MODEL_PRESETS,
                       "providers": providers_public()},
            "skills": {"session": c.get("skills", []), "plugged": scan_skills()},
            "roles": load_roles(),
            "events": load_events(),
            "collab": load_collab(),
            "scenarios": scan_scenarios(),
            "schedules": load_schedules(),
            "email_configured": bool(email_cfg() and email_cfg().get("smtp_host")),
            "transcribe_configured": bool((c.get("transcribe") or {}).get("command")),
            "customers": scan_customers(),
            "projects": scan_projects(c),
            "archived": scan_archived(),
            "recent": recent_files(limit=12),
            "knowledge": scan_tree("knowledge"),
            "extract": extract_stats(),
            "inbox": scan_tree("inbox"),
        }

    def create_customer(self, c, body):
        name = slug(body.get("name", ""))
        if not name:
            raise ValueError("客户名不能为空")
        d = ROOT / "customers" / name
        if d.exists():
            raise ValueError(f"客户已存在: {name}")
        (d / "meetings").mkdir(parents=True)
        (d / "customer.json").write_text(json.dumps({
            "full_name": name, "industry": "", "sales": "", "level": "重点", "address": "",
            "website": "", "org": "", "contacts": [], "kpis": [],
            "updated": now_iso()}, ensure_ascii=False, indent=2), encoding="utf-8")
        pid, pdir, manifest = active_pack(c)
        rel = manifest.get("files", {}).get("customer-profile")
        profile = d / "profile.md"
        if rel and (pdir / rel).exists() and (pdir / rel).suffix.lower() == ".md":
            content = read_text(pdir / rel).replace("【客户全称】", name)
            profile.write_text(content, encoding="utf-8")
        else:
            profile.write_text(
                f"# 客户档案 · {name}\n\n> 当前模板包({pid})未挂载 customer-profile 槽位。"
                f"请上传你的客户档案模板,或直接在此填写。\n", encoding="utf-8")
        return {"ok": True, "id": name}

    def create_project(self, c, body):
        name = slug(body.get("name", ""))
        customer = slug(body.get("customer", ""))
        stage = body.get("stage") or "调研"
        if not name or not customer:
            raise ValueError("项目名与客户均不能为空")
        d = ROOT / "projects" / f"{customer}-{name}"
        if d.exists():
            raise ValueError(f"项目已存在: {d.name}")
        d.mkdir(parents=True)
        (d / "meetings").mkdir(exist_ok=True)
        (d / "project.json").write_text(json.dumps(
            {"name": name, "customer": customer, "stage": stage, "created": now_iso()},
            ensure_ascii=False, indent=2), encoding="utf-8")
        return {"ok": True, "dir": d.name}

    def create_meeting(self, body):
        """纪要与项目绑定:落到 projects/<项目>/meetings/。"""
        project = slug(body.get("project", ""))
        if not project:
            raise ValueError("每一篇纪要必须绑定一个项目")
        pdir = ROOT / "projects" / project
        if not pdir.is_dir():
            raise FileNotFoundError(f"项目不存在: {project}")
        meta = wb.load_json(pdir / "project.json") or {}
        customer = meta.get("customer", "")
        topic = slug(body.get("topic", "")) or "拜访"
        date = body.get("date") or datetime.now().strftime("%Y-%m-%d")
        d = pdir / "meetings"
        d.mkdir(exist_ok=True)
        p, i = d / f"{date}-{topic}.md", 2
        while p.exists():
            p = d / f"{date}-{topic}-{i}.md"
            i += 1
        # 结构化字段(纪要表单)→ 组装 Markdown;未提供则给骨架
        pts = [x.strip() for x in (body.get("points") or "").splitlines() if x.strip()]
        ccs = [x.strip() for x in (body.get("concerns") or "").splitlines() if x.strip()]
        acts = [x.strip() for x in (body.get("actions") or "").splitlines() if x.strip()]
        L = [f"# {topic}", "", f"- 日期:{date}", f"- 客户:{customer}",
             f"- 参会(客户方):{body.get('att_cust', '')}",
             f"- 参会(我方):{body.get('att_us', '')}", "", "## 要点", ""]
        L += [f"- {x}" for x in pts] or ["- "]
        L += ["", "## 客户关注 / 异议", ""]
        L += [f"- {x}" for x in ccs] or ["- "]
        L += ["", "## 行动项", ""]
        L += [f"- [ ] {x}" for x in acts] or ["- [ ] 【事项】@【负责人】【截止】"]
        p.write_text("\n".join(L) + "\n", encoding="utf-8")
        return {"ok": True, "path": str(p.relative_to(ROOT))}

    def instantiate_slot(self, c, body):
        proj = slug(body.get("project", ""))
        slot = body.get("slot", "")
        d = ROOT / "projects" / proj
        if not d.is_dir():
            raise FileNotFoundError(f"项目不存在: {proj}")
        src, tpl = find_template(c, slot, body.get("template_id"))
        label = next((s["label"] for s in c.get("pipeline", []) if s["slot"] == slot), slot)
        pref = slot_prefix(slot) or "00"
        dst = d / f"{pref}-{label}_v1{src.suffix}"
        i = 2
        while dst.exists():
            dst = d / f"{pref}-{label}_v{i}{src.suffix}"
            i += 1
        shutil.copy2(src, dst)
        return {"ok": True, "path": str(dst.relative_to(ROOT)),
                "template": tpl["name"]}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8917)
    ap.add_argument("--host", default="127.0.0.1",
                    help="默认仅本机;共享给同事需自担风险再改")
    args = ap.parse_args()

    # 确保数据区骨架存在(新机器/新同事克隆后开箱即用)
    for d in ["customers", "projects", "inbox", "archive", "scenarios", "calendar",
              "collab", "knowledge/my local knowledge", "knowledge/reports"]:
        (ROOT / d).mkdir(parents=True, exist_ok=True)

    # 后台线程:日程提醒(macOS 通知)+ 定时 AI 任务调度
    threading.Thread(target=reminder_loop, daemon=True).start()
    threading.Thread(target=scheduler_loop, daemon=True).start()

    # 启动即同步一次插件(mcp 合并 + skills 复制)
    try:
        c = cfg()
        state = wb.collect(c)
        wb.write_mcp(state)
        wb.copy_skills(state)
    except Exception as e:  # noqa: BLE001
        print(f"[workbench] 插件同步警告: {e}")

    load_job_history()
    srv = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"[workbench] ✅ 售前工作台已启动: http://{args.host}:{args.port}")
    print(f"[workbench] 根目录: {ROOT}")
    print(f"[workbench] claude CLI: {'可用' if claude_available() else '未找到(AI 功能停用)'}")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n[workbench] 已停止")


if __name__ == "__main__":
    main()
