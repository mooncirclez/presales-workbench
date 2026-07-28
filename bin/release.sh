#!/usr/bin/env bash
# 发版:改版本号 → 提交 → 打标签 → 推送。标签推上去后 GitHub Actions 自动建 Release。
#
#   bin/release.sh 0.3.0          # 发布 0.3.0
#   bin/release.sh 0.3.0 --dry    # 只做检查,不改任何东西、不推送
#
# 前提:CHANGELOG.md 里已经写好该版本那一段(没写会直接拦下来)。
set -euo pipefail
cd "$(dirname "$0")/.."

VER="${1:-}"
DRY="${2:-}"
[ -n "$VER" ] || { echo "用法:bin/release.sh <版本号> [--dry]   例:bin/release.sh 0.3.0"; exit 1; }
VER="${VER#v}"

# 版本号必须是 x.y.z,否则标签和 workbench.json 会对不上
[[ "$VER" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || { echo "✗ 版本号要写成 x.y.z 形式,收到:$VER"; exit 1; }

echo "▶ 发布 v$VER"

# 1. 日志必须先写好 —— 发布说明就是从这里取的
python3 bin/changelog.py --check "$VER" || {
  echo "✗ CHANGELOG.md 里还没有 $VER 这一段,先把更新内容写进去再发版"; exit 1; }
echo "  ✓ CHANGELOG 已就绪:$(python3 bin/changelog.py "$VER" --title)"

# 2. 标签不能重复
if git rev-parse "v$VER" >/dev/null 2>&1; then
  echo "✗ 标签 v$VER 已存在。改个版本号,或先删:git tag -d v$VER && git push origin :refs/tags/v$VER"; exit 1
fi

# 3. 工作区必须干净,避免把没想好的改动一起发出去
if [ -n "$(git status --porcelain)" ]; then
  echo "✗ 有未提交的改动,先提交或撤销:"; git status --short; exit 1
fi

# 4. 必须在 main 上,且与远端同步
BR=$(git rev-parse --abbrev-ref HEAD)
[ "$BR" = "main" ] || { echo "✗ 当前在 $BR 分支,发版请切到 main"; exit 1; }
git fetch -q origin
if [ "$(git rev-parse HEAD)" != "$(git rev-parse origin/main)" ]; then
  echo "✗ 本地与远端 main 不一致,先 git pull / git push 对齐"; exit 1
fi
echo "  ✓ 工作区干净,main 与远端一致"

if [ "$DRY" = "--dry" ]; then echo "▶ --dry:检查全过,未做任何改动"; exit 0; fi

# 5. 版本号写进配置(界面侧栏读这里,也是 CI 的一致性校验对象)
python3 - "$VER" <<'PY'
import collections, json, pathlib, sys
p = pathlib.Path("workbench.json")
c = json.loads(p.read_text(encoding="utf-8"), object_pairs_hook=collections.OrderedDict)
c["version"] = sys.argv[1]
p.write_text(json.dumps(c, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
PY
git add workbench.json
git commit -q -m "chore(release): v$VER"
echo "  ✓ 版本号已写入 workbench.json 并提交"

# 6. 打附注标签(标签说明用日志正文,即使不建 Release 也能看到内容)
python3 bin/changelog.py "$VER" > /tmp/wb-release-notes.md
{ python3 bin/changelog.py "$VER" --title; echo; cat /tmp/wb-release-notes.md; } \
  | git tag -a "v$VER" -F -
rm -f /tmp/wb-release-notes.md

git push -q origin main
git push -q origin "v$VER"
echo "  ✓ 已推送 main 与标签 v$VER"
echo
echo "▶ 完成。GitHub Actions 正在自动建 Release:"
echo "   https://github.com/mooncirclez/presales-workbench/actions"
echo "   https://github.com/mooncirclez/presales-workbench/releases"
