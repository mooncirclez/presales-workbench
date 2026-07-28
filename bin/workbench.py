#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""售前工作台 · 插件同步工具(纯标准库,Python 3.9+)

用法:
  python3 bin/workbench.py sync    # 重建 .mcp.json、.claude/skills/
  python3 bin/workbench.py status  # 打印当前插槽状态(JSON)

工作台界面请用: python3 bin/server.py
"""
import json
import shutil
import socket
import sys
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parent.parent


def die(msg):
    print(f"[workbench] 错误: {msg}")
    sys.exit(1)


def load_json(path):
    p = Path(path)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        die(f"JSON 解析失败 {p}: {e}")


def probe_http(url, timeout=0.4):
    """本地探活:仅 TCP 连接测试,不发请求。"""
    try:
        u = urlparse(url)
        host = u.hostname or "127.0.0.1"
        port = u.port or (443 if u.scheme == "https" else 80)
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def collect(cfg):
    state = {"generated": datetime.now().strftime("%Y-%m-%d %H:%M")}

    tdir = ROOT / cfg.get("templates", {}).get("dir", "plugins/templates")
    pack_id = cfg.get("templates", {}).get("active_pack", "")
    pack_dir = tdir / pack_id
    manifest = load_json(pack_dir / "manifest.json") or {}
    files = manifest.get("files", {})
    pipeline = []
    for step in cfg.get("pipeline", []):
        rel = files.get(step["slot"])
        mounted = bool(rel) and (pack_dir / rel).exists()
        pipeline.append({"slot": step["slot"], "label": step["label"],
                         "stage": step.get("stage", ""),
                         "file": Path(rel).name if rel else None, "mounted": mounted})
    packs = sorted(d.name for d in tdir.iterdir()
                   if d.is_dir() and (d / "manifest.json").exists()) if tdir.is_dir() else []
    state["pack"] = {"id": pack_id, "version": manifest.get("version", "?"),
                     "label": manifest.get("label", ""), "origin": manifest.get("origin", ""),
                     "note": manifest.get("note", ""), "available": packs,
                     "mounted": sum(1 for s in pipeline if s["mounted"]), "total": len(pipeline)}
    state["pipeline"] = pipeline

    mdir = ROOT / cfg.get("mcp", {}).get("dir", "plugins/mcp")
    merged, mcps = {}, []
    for mid in cfg.get("mcp", {}).get("enabled", []):
        frag = load_json(mdir / f"{mid}.json")
        if not frag:
            mcps.append({"id": mid, "status": "missing", "url": ""})
            continue
        for name, server in frag.get("mcpServers", {}).items():
            merged[name] = server
            url = server.get("url", "")
            status = ("live" if probe_http(url) else "offline") if url else "stdio"
            mcps.append({"id": name, "status": status, "url": url})
    state["mcp"] = mcps
    state["_merged_mcp"] = merged

    plugged = []
    sdir = ROOT / "plugins/skills"
    if sdir.is_dir():
        plugged = sorted(d.name for d in sdir.iterdir()
                         if d.is_dir() and (d / "SKILL.md").exists())
    state["skills"] = {"session": cfg.get("skills", []), "plugged": plugged}
    state["knowledge"] = cfg.get("knowledge", {})
    return state


def write_mcp(state):
    out = ROOT / ".mcp.json"
    out.write_text(
        json.dumps({"mcpServers": state["_merged_mcp"]}, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8")
    return out


def copy_skills(state):
    dst = ROOT / ".claude" / "skills"
    for name in state["skills"]["plugged"]:
        shutil.copytree(ROOT / "plugins/skills" / name, dst / name, dirs_exist_ok=True)
    return state["skills"]["plugged"]


def main():
    cfg = load_json(ROOT / "workbench.json")
    if cfg is None:
        die("找不到 workbench.json")
    cmd = sys.argv[1] if len(sys.argv) > 1 else "sync"
    state = collect(cfg)

    if cmd == "status":
        print(json.dumps({k: v for k, v in state.items() if k != "_merged_mcp"},
                         ensure_ascii=False, indent=2))
        return
    if cmd != "sync":
        die(f"未知命令 {cmd}(可用:sync / status)")

    mcp_path = write_mcp(state)
    skills = copy_skills(state)
    p = state["pack"]
    print("[workbench] ✅ sync 完成")
    print(f"  模板包   {p['id']}@{p['version']}  流水线 {p['mounted']}/{p['total']} 已挂载")
    print(f"  MCP      {', '.join(m['id'] + '(' + m['status'] + ')' for m in state['mcp']) or '无'}"
          f"  → {mcp_path.name}")
    print(f"  Skills   插槽 {len(skills)} 个 → .claude/skills/")
    print("  界面     python3 bin/server.py → http://127.0.0.1:8917")


if __name__ == "__main__":
    main()
