#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""售前工作台 · 客户侧代号化(纯标准库,Python 3.9+)

设计前提(重要,改这个文件前先读):

  1. **只有客户侧实体进映射表。** 客户名、客户方联系人/决策人、客户地址。
     我方同事(销售/产研/交付对接人)**不登记**,因此天然不会被替换 ——
     映射表本身就是"该脱敏"的白名单,不需要另一套排除规则。

  2. **磁盘上存的就是代号。** 不做"明文存盘 + 出口脱敏",而是写入即代号化。
     真名只存在 .mask/map.json 一处。少一个明文副本 = 少一条泄露路径,
     且 AI 读磁盘时天然只能看到代号,不需要影子目录。

  3. **可读假名,不是不可读代号。** 示例银行 → sl银行,张三丰 → 张sf。
     保留"是什么"(银行/证券/姓),抹掉"是谁"。
     **代价**:比 @C1 好认得多,保护也弱得多 —— 「sl银行」在银行业基本能被猜出来。
     这是有意的可读性取舍,对外说明保护强度时要按 sl银行 的实际强度说。

  4. **导出不自动还原。** 用户明确要求导出后自己做一层人工处理。
     `table` 子命令输出替换清单,拿去做全局查找替换即可。

用法:
  python3 bin/mask.py list                  # 列出全部映射
  python3 bin/mask.py table                 # 导出替换清单(给人工全局替换用)
  python3 bin/mask.py lookup sl银行          # 查假名对应的真名
  python3 bin/mask.py code 示例银行           # 查真名对应的假名(不存在则不分配)
  python3 bin/mask.py alias sl银行 示例行 示例   # 补别名(简称要手工补,否则漏替换)
  python3 bin/mask.py mask   <file>         # 把文件里的真名换成假名(改写原文件)
  python3 bin/mask.py unmask <file>         # 反向还原(打印到 stdout,不改原文件)
"""
import fcntl
import json
import os
import re
import shutil
import sys
import threading
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MASK_DIR = ROOT / ".mask"
MAP_FILE = MASK_DIR / "map.json"
BACKUP_DIR = MASK_DIR / "backups"

KINDS = {"customer": "C", "contact": "P", "address": "A"}

# 可读脱敏:保留"是什么",抹掉"是谁"
#   示例银行 → sl银行      (机构后缀留着,前面转拼音首字母)
#   张三丰   → 张sf        (姓留着,名转拼音首字母)
# 权衡:比 @C1 好认得多,保护也弱得多 —— 「sl银行」在银行业基本能被猜出来。
# 这是用户明确要的可读性,不是安全上的最优解。
ORG_SUFFIX = [
    "股份有限公司", "有限责任公司", "有限公司", "集团有限公司",
    "交易所", "研究院", "研究所", "设计院", "事业部", "分公司", "子公司",
    "集团", "银行", "证券", "保险", "基金", "信托", "期货", "租赁",
    "科技", "软件", "信息", "数据", "通信", "电子", "网络", "智能",
    "医院", "大学", "学院", "中心", "公司", "分行", "支行", "总行",
]
# 复姓表:没有它「欧阳明」会被切成「欧」+「阳明」
COMPOUND_SURNAMES = [
    "欧阳", "司马", "上官", "夏侯", "诸葛", "东方", "皇甫", "尉迟",
    "公孙", "慕容", "长孙", "宇文", "司徒", "鲜于", "闾丘", "太叔", "申屠",
]


# ---------------- 映射表读写 ----------------

class MaskError(Exception):
    """映射表不可用。**必须 fail closed** —— 脱敏失败时宁可写入报错,
    也不能静默放行把真名落盘。用普通 Exception 而不是 SystemExit,
    否则在 server 的请求线程里会直接把线程干掉、前端只看到连接断开。"""


# 映射表的每个写操作都是「读全表 → 改 → 写回」。服务是多线程的
# (ThreadingHTTPServer),CLI 又是另一个进程 —— 不加锁就会互相覆盖。
# **实测踩过**:两条并发写把已登记的两个实体整个抹掉了,而且悄无声息。
_TLOCK = threading.RLock()
LOCK_FILE = MASK_DIR / ".lock"


@contextmanager
def locked():
    """线程锁 + 文件锁。前者管同进程内的多线程,后者管 CLI 与服务并存。"""
    with _TLOCK:
        MASK_DIR.mkdir(parents=True, exist_ok=True)
        with open(LOCK_FILE, "w") as f:
            fcntl.flock(f.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(f.fileno(), fcntl.LOCK_UN)


def _blank():
    return {"version": 1, "next": {}, "entities": {}}


def load_map():
    if not MAP_FILE.exists():
        return _blank()
    try:
        d = json.loads(MAP_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        # 映射表损坏时绝不静默重建 —— 那等于永久丢失所有真名
        raise MaskError(f"映射表损坏,拒绝继续:{MAP_FILE}({e})。"
                        f"请从 {BACKUP_DIR} 里挑一份备份恢复。") from e
    if not isinstance(d, dict) or "entities" not in d:
        raise MaskError(f"映射表结构异常:{MAP_FILE}")
    d.setdefault("version", 1)
    d.setdefault("next", {})
    return d


def save_map(d):
    """每次写入前留一份带时间戳的备份 —— 映射表是单点,丢了数据永久匿名。"""
    MASK_DIR.mkdir(parents=True, exist_ok=True)
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    if MAP_FILE.exists():
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        try:
            shutil.copy2(MAP_FILE, BACKUP_DIR / f"map-{stamp}.json")
        except OSError:
            pass
        _prune_backups()
    tmp = MAP_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(d, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, MAP_FILE)          # 原子替换,避免写一半被中断
    for p in (MAP_FILE, MASK_DIR):
        try:
            os.chmod(p, 0o600 if p.is_file() else 0o700)
        except OSError:
            pass


def _prune_backups(keep=60):
    files = sorted(BACKUP_DIR.glob("map-*.json"))
    for p in files[:-keep]:
        try:
            p.unlink()
        except OSError:
            pass


# ---------------- 代号分配与查询 ----------------

def _initials(s):
    """中文取拼音首字母;非中文原样保留(如 IBM、A股)。"""
    if not s:
        return ""
    try:
        from pypinyin import lazy_pinyin, Style
        out = "".join(lazy_pinyin(s, style=Style.FIRST_LETTER))
    except ImportError:
        # 没装 pypinyin 就退回不可读代号 —— 宁可难看,也不能不脱敏
        return ""
    return re.sub(r"[^A-Za-z0-9]", "", out).lower()


def _split_surname(name):
    for c in COMPOUND_SURNAMES:
        if name.startswith(c) and len(name) > len(c):
            return c, name[len(c):]
    return (name[0], name[1:]) if len(name) > 1 else (name, "")


def _readable(real, kind):
    """生成可读假名。失败(拿不到拼音)时返回空,由调用方退回不可读代号。"""
    real = real.strip()
    if kind == "contact":
        surname, given = _split_surname(real)
        ini = _initials(given)
        return f"{surname}{ini}" if ini else ""
    # 机构:保留最长匹配的后缀,前半段转首字母
    for suf in sorted(ORG_SUFFIX, key=len, reverse=True):
        if real.endswith(suf) and len(real) > len(suf):
            ini = _initials(real[: -len(suf)])
            return f"{ini}{suf}" if ini else ""
    ini = _initials(real)          # 没有可识别后缀,如「华为」→ hw
    return ini


def _next_code(d, kind, parent=None, real=""):
    used = set(d["entities"])
    cand = _readable(real, kind) if real else ""
    if cand:
        # 冲突消解:示例银行 与 双林银行 首字母都是 sl,后来者补数字
        if cand not in used:
            return cand
        n = 2
        while f"{cand}{n}" in used:
            n += 1
        return f"{cand}{n}"
    # 兜底:拿不到拼音时用不可读代号,绝不原样落盘
    pre = KINDS.get(kind, "C")
    n = int(d["next"].get(pre, 0)) + 1
    while f"@{pre}{n}" in used:
        n += 1
    d["next"][pre] = n
    return f"@{pre}{n}"


def code_for(real, kind="customer", parent=None, d=None, create=True):
    """真名 → 代号。已登记则复用(代号必须稳定,否则历史数据全对不上)。"""
    real = (real or "").strip()
    if not real:
        return "", d
    if d is not None:                      # 调用方自己持锁并负责保存
        return _code_for_unlocked(real, kind, parent, d, create)
    with locked():
        d = load_map()
        code = _code_for_unlocked(real, kind, parent, d, create)[0]
        if code and code not in ("",):
            save_map(d)
        return code, d


def _code_for_unlocked(real, kind, parent, d, create):
    if real in d["entities"]:              # 已经是假名,别二次编码
        return real, d
    for code, e in d["entities"].items():
        if e.get("real") == real or real in (e.get("aliases") or []):
            return code, d
    if not create:
        return "", d
    code = _next_code(d, kind, parent, real=real)
    d["entities"][code] = {"kind": kind, "real": real, "aliases": [],
                           "parent": parent or "", "created": datetime.now().isoformat(timespec="seconds")}
    return code, d


def redact(real, label, kind="misc", d=None):
    """强识别物 → 带标签的占位符,如「sl银行-网址」。

    和 code_for 的区别:地址、域名、电话这类东西**没有"有意义的可读假名"**
    (拼音首字母只会变成一串乱码),但它们本身就能认出客户是谁。
    所以整体换成占位符,真值进映射表,要用时查表。
    """
    real = (real or "").strip()
    if not real:
        return real
    if d is None:
        with locked():
            d2 = load_map()
            code = _redact_unlocked(real, label, kind, d2)
            save_map(d2)
            return code
    return _redact_unlocked(real, label, kind, d)


def _redact_unlocked(real, label, kind, d):
    if real in d["entities"]:
        return real                       # 已经是占位符,不二次处理
    for code, e in d["entities"].items():
        if e.get("real") == real:
            return code
    code, n = label, 2
    while code in d["entities"]:
        code = f"{label}{n}"
        n += 1
    d["entities"][code] = {"kind": kind, "real": real, "aliases": [],
                           "parent": "", "created": datetime.now().isoformat(timespec="seconds")}
    return code


def update_entry(code, real=None, kind=None, aliases=None, d=None):
    """改一条已有映射。别名整体覆盖(界面上是一个输入框,所见即所得)。"""
    if d is None:
        with locked():
            d2 = load_map()
            _update_unlocked(code, real, kind, aliases, d2)
            save_map(d2)
            return d2
    return _update_unlocked(code, real, kind, aliases, d)


def _update_unlocked(code, real, kind, aliases, d):
    e = d["entities"].get(code)
    if not e:
        raise MaskError(f"假名不存在:{code}")
    if real is not None and real.strip():
        e["real"] = real.strip()
    if kind:
        e["kind"] = kind
    if aliases is not None:
        e["aliases"] = [a for a in
                        (x.strip() for x in aliases) if a and a != e.get("real")]
    return d


def remove_entry(code, d=None):
    """删一条映射。**这是不可逆的**:已经落盘的文件里还留着这个假名,
    删掉映射之后就再也查不出它原本是谁了。界面上必须二次确认。"""
    if d is None:
        with locked():
            d2 = load_map()
            if code not in d2["entities"]:
                raise MaskError(f"假名不存在:{code}")
            gone = d2["entities"].pop(code)
            save_map(d2)
            return gone
    if code not in d["entities"]:
        raise MaskError(f"假名不存在:{code}")
    return d["entities"].pop(code)


def real_for(code, d=None):
    d = d or load_map()
    e = d["entities"].get(code)
    return (e or {}).get("real", "")


def is_code(s, d=None):
    """假名可读之后就没有固定前缀了,只能查表判断,不能靠正则。"""
    s = (s or "").strip()
    if not s:
        return False
    d = d or load_map()
    return s in d["entities"]


def add_alias(code, aliases, d=None):
    if d is None:
        with locked():
            d2 = load_map()
            _add_alias_unlocked(code, aliases, d2)
            save_map(d2)
            return d2
    return _add_alias_unlocked(code, aliases, d)


def _add_alias_unlocked(code, aliases, d):
    e = d["entities"].get(code)
    if not e:
        raise MaskError(f"假名不存在:{code}")
    cur = e.setdefault("aliases", [])
    for a in aliases:
        a = a.strip()
        if a and a not in cur and a != e.get("real"):
            cur.append(a)
    return d


# ---------------- 文本替换 ----------------

def _pairs(d, reverse=False):
    """返回 (查找串, 替换串) 列表,**按查找串长度倒序**。

    两个方向都必须按"查找串"排序,踩过的坑:
      · 正向漏了会让「示例」先吃掉「示例证券」
      · 反向漏了会让「@C1」先吃掉「@C1-P1」,还原出「示例证券-P1」这种残骸
    反向只用规范真名,不带别名 —— 一个代号必须只还原成一个名字。
    """
    out = []
    for code, e in d["entities"].items():
        real = e.get("real", "")
        if reverse:
            if real:
                out.append((code, real))
        else:
            for n in [real] + list(e.get("aliases") or []):
                if n:
                    out.append((n, code))
    return sorted(out, key=lambda kv: len(kv[0]), reverse=True)


def mask_text(s, d=None):
    """真名 → 代号。用于写盘、生成 .extracted 伴生文本。"""
    if not s:
        return s
    d = d or load_map()
    for real, code in _pairs(d):
        s = s.replace(real, code)
    return s


def unmask_text(s, d=None):
    """代号 → 真名。用于导出 docx/md、发邮件 —— 交给客户的东西必须是真名。"""
    if not s:
        return s
    d = d or load_map()
    for code, real in _pairs(d, reverse=True):
        s = s.replace(code, real)
    return s


def mask_query(q, d=None):
    """搜索用:把查询词里的真名换成代号,否则搜『光大』在磁盘上永远 0 命中。"""
    return mask_text(q, d)


def mask_name(name, d=None):
    """文件名/目录名专用:只替换,不做任何路径规范化。"""
    return mask_text(name, d)


def stats(d=None):
    d = d or load_map()
    n = {}
    for e in d["entities"].values():
        n[e.get("kind", "?")] = n.get(e.get("kind", "?"), 0) + 1
    return {"total": len(d["entities"]), "by_kind": n,
            "map_file": str(MAP_FILE), "exists": MAP_FILE.exists()}


# ---------------- CLI ----------------

def _cli():
    a = sys.argv[1:]
    if not a:
        print(__doc__)
        return 0
    cmd = a[0]

    if cmd == "list":
        d = load_map()
        if not d["entities"]:
            print("(映射表为空)")
            return 0
        w = max(len(c) for c in d["entities"])
        for code, e in sorted(d["entities"].items()):
            al = ("  别名:" + "、".join(e["aliases"])) if e.get("aliases") else ""
            print(f"  {code:<{w}}  {e.get('kind',''):<8} {e.get('real','')}{al}")
        s = stats(d)
        print(f"\n  共 {s['total']} 条 · {s['by_kind']}")
        return 0

    if cmd == "table":
        # 导出后人工全局替换用。默认 TSV(可直接粘进 Excel);加 --md 出 Markdown 表。
        d = load_map()
        rows = sorted(d["entities"].items(), key=lambda kv: (kv[1].get("kind", ""), kv[0]))
        if not rows:
            print("(映射表为空)")
            return 0
        md = "--md" in a
        if md:
            print("| 文档里的假名 | 替换成 | 类型 |")
            print("|---|---|---|")
            for code, e in rows:
                print(f"| `{code}` | {e.get('real','')} | {e.get('kind','')} |")
        else:
            print("假名\t真名\t类型")
            for code, e in rows:
                print(f"{code}\t{e.get('real','')}\t{e.get('kind','')}")
        print(f"\n# 共 {len(rows)} 条。导出的交付物里出现左列,替换成右列。", file=sys.stderr)
        print("# 注意:替换要**从长到短**做,否则「sl银行」会被「sl银行2」的前半截干扰。",
              file=sys.stderr)
        return 0

    if cmd == "lookup" and len(a) > 1:
        r = real_for(a[1])
        print(r or f"(未找到 {a[1]})")
        return 0 if r else 1

    if cmd == "code" and len(a) > 1:
        c, _ = code_for(a[1], create=False)
        print(c or f"(未登记 {a[1]})")
        return 0 if c else 1

    if cmd == "alias" and len(a) > 2:
        add_alias(a[1], a[2:])
        print(f"✓ {a[1]} 已加别名:{'、'.join(a[2:])}")
        return 0

    if cmd in ("mask", "unmask") and len(a) > 1:
        p = Path(a[1])
        if not p.exists():
            print(f"✗ 文件不存在:{p}", file=sys.stderr)
            return 1
        s = p.read_text(encoding="utf-8")
        out = mask_text(s) if cmd == "mask" else unmask_text(s)
        if cmd == "mask":
            p.write_text(out, encoding="utf-8")
            print(f"✓ 已代号化写回 {p}")
        else:
            sys.stdout.write(out)
        return 0

    print(__doc__)
    return 1


if __name__ == "__main__":
    sys.exit(_cli())
