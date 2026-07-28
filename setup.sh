#!/bin/bash
# 售前工作台 · 一键部署
# 用法:./setup.sh          检查环境 → 装可选依赖 → 建目录骨架 → 启动
#      ./setup.sh --check  只做环境自检,不改动任何东西
set -uo pipefail

RED='\033[0;31m'; GRN='\033[0;32m'; YEL='\033[1;33m'; CYA='\033[0;36m'; B='\033[1m'; N='\033[0m'
ok(){ printf "${GRN}✓${N} %s\n" "$*"; }
warn(){ printf "${YEL}!${N} %s\n" "$*"; }
err(){ printf "${RED}✗${N} %s\n" "$*" >&2; }
info(){ printf "${CYA}→${N} %s\n" "$*"; }
section(){ printf "\n${B}%s${N}\n" "$*"; }

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"
CHECK_ONLY=false
[ "${1:-}" = "--check" ] && CHECK_ONLY=true

FATAL=0; DEGRADED=0

section "售前工作台 · 环境自检"

# ── 硬性前置条件 ────────────────────────────────────────────
if [ "$(uname)" = "Darwin" ]; then
  ok "操作系统 macOS $(sw_vers -productVersion 2>/dev/null)"
else
  err "本项目目前**仅支持 macOS**(依赖 textutil / osascript 等系统命令)"
  err "  当前系统:$(uname) —— Windows/Linux 支持见 README「路线图」"
  FATAL=1
fi

PY=""
for c in python3 python; do
  if command -v $c >/dev/null 2>&1; then
    v=$($c -c 'import sys;print("%d.%d"%sys.version_info[:2])' 2>/dev/null)
    if [ -n "$v" ] && [ "$(printf '%s\n3.9\n' "$v" | sort -V | head -1)" = "3.9" ]; then
      PY=$c; ok "Python $v ($(command -v $c))"; break
    fi
  fi
done
[ -z "$PY" ] && { err "需要 Python 3.9+(macOS 自带,或 brew install python3)"; FATAL=1; }

if command -v claude >/dev/null 2>&1; then
  ok "Claude Code CLI $(claude --version 2>/dev/null | head -1)"
  if [ "$CHECK_ONLY" = false ]; then
    info "验证登录态(约 10 秒)…"
    out=$(env -u CLAUDECODE -u CLAUDE_CODE_ENTRYPOINT claude -p "回复 OK" --max-turns 1 2>&1 </dev/null | tail -2)
    if echo "$out" | grep -qi "oauth\|authenticat\|401\|logged out"; then
      warn "claude 未登录 —— 运行 ${B}claude${N} 后输入 ${B}/login${N} 完成授权;AI 功能在此之前不可用"
      DEGRADED=1
    else
      ok "claude 登录正常,AI 功能就绪"
    fi
  fi
else
  err "未找到 claude CLI —— 本工作台的 AI 能力依赖它"
  err "  安装:https://claude.com/claude-code  之后运行 claude 并 /login"
  FATAL=1
fi

# ── 可选能力(缺失只降级,不阻塞) ──────────────────────────
section "可选能力"
if command -v pdftotext >/dev/null 2>&1; then ok "pdftotext —— PDF 文本提取"
else warn "缺 pdftotext —— PDF 无法提取(brew install poppler);其他格式不影响"; DEGRADED=1; fi

check_py_mod(){ $PY -c "import $1" >/dev/null 2>&1; }
PIPMODS=()
check_py_mod docx     || PIPMODS+=("python-docx")
check_py_mod openpyxl || PIPMODS+=("openpyxl")
check_py_mod pptx     || PIPMODS+=("python-pptx")

if [ ${#PIPMODS[@]} -eq 0 ]; then
  ok "python-docx / openpyxl / python-pptx —— Word 导出与 Office 文档提取"
else
  warn "缺少 Python 库:${PIPMODS[*]}"
  if [ "$CHECK_ONLY" = true ]; then
    info "  安装:pip3 install --user ${PIPMODS[*]}"
    DEGRADED=1
  else
    printf "  现在安装?(缺少时 Word 导出与 Office 提取会降级) [Y/n] "
    read -r yn
    if [[ ! "$yn" =~ ^[Nn] ]]; then
      $PY -m pip install --user --quiet "${PIPMODS[@]}" 2>&1 | tail -2
      check_py_mod docx && ok "已安装" || { warn "安装未完全成功,可稍后手动执行:pip3 install --user ${PIPMODS[*]}"; DEGRADED=1; }
    else
      DEGRADED=1
    fi
  fi
fi

if [ "$FATAL" -ne 0 ]; then
  section "自检未通过"
  err "请先解决上面标 ✗ 的项,再重新运行 ./setup.sh"
  exit 1
fi
[ "$CHECK_ONLY" = true ] && { section "自检通过"; [ "$DEGRADED" -ne 0 ] && warn "部分可选能力缺失(见上),核心功能可用"; exit 0; }

# ── 建目录骨架 ─────────────────────────────────────────────
section "初始化数据目录"
for d in customers projects archive scenarios calendar collab inbox \
         "knowledge/my local knowledge" knowledge/reports .workbench; do
  mkdir -p "$d"
done
ok "数据目录就绪"

# ── 个性化 workbench.json ─────────────────────────────────
if grep -q '"owner": "你的名字"' workbench.json 2>/dev/null; then
  section "个性化配置"
  printf "  你的名字(回车跳过): "; read -r who
  printf "  公司名称(回车跳过): "; read -r comp
  [ -n "$who" ] || [ -n "$comp" ] && $PY - "$who" "$comp" <<'PYEOF'
import json, sys, pathlib
who, comp = (sys.argv[1] or "").strip(), (sys.argv[2] or "").strip()
p = pathlib.Path("workbench.json"); c = json.loads(p.read_text(encoding="utf-8"))
if who: c["owner"] = who
if comp: c.setdefault("company", {})["name"] = comp
p.write_text(json.dumps(c, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
print("  ✓ 已写入 workbench.json")
PYEOF
fi

# ── 启动 ───────────────────────────────────────────────────
section "启动工作台"
PORT="${PORT:-8917}"
if lsof -nP -iTCP:$PORT -sTCP:LISTEN >/dev/null 2>&1; then
  warn "端口 $PORT 已被占用 —— 可能工作台已在运行"
  info "换端口启动:PORT=8919 ./setup.sh"
else
  nohup $PY bin/server.py --port "$PORT" >> .workbench/server.log 2>&1 &
  disown
  sleep 2
  if lsof -nP -iTCP:$PORT -sTCP:LISTEN >/dev/null 2>&1; then
    ok "已启动"
  else
    err "启动失败,查看日志:tail -20 .workbench/server.log"; exit 1
  fi
fi

command -v open >/dev/null 2>&1 && open "http://127.0.0.1:$PORT" 2>/dev/null

section "完成"
printf "  工作台     ${B}http://127.0.0.1:%s${N}\n" "$PORT"
printf "  数据位置   %s\n" "$ROOT"
printf "  停止服务   kill \$(lsof -nP -tiTCP:%s -sTCP:LISTEN)\n" "$PORT"
[ "$DEGRADED" -ne 0 ] && printf "\n${YEL}提示${N}:部分可选能力缺失,核心功能不受影响。重跑 ./setup.sh --check 可复查。\n"
printf "\n下一步:把你的资料放进 ${B}knowledge/my local knowledge/${N},再到「知识库」页点「提取新增」。\n"
printf "当前是 demo 数据,可在各页面用垃圾桶图标删除后录入你自己的客户与项目。\n\n"
