# Skill 插槽

放**随工作台走的自定义 Skill**,换电脑/换公司/分享给同事时跟着仓库一起走。

## 结构
```text
plugins/skills/
└── <skill-name>/
    └── SKILL.md      # 技能定义(frontmatter: name + description,正文为操作指引)
```

`python3 bin/workbench.py sync` 会把这里的技能复制到 `.claude/skills/`,Claude Code 即可识别。

## 与会话级 Skill 的关系
- **会话级**(wecomcli-*、huashu-design、docx/pptx/xlsx/pdf、dataviz):由 Claude 环境提供,开箱即用,但不随仓库走。
- **本目录**:你自己沉淀的售前专属技能(如"按公司规范写立项报告"的固化流程),可版本管理、可分发。

想固化某个流程时,直接对 Claude 说:"把刚才这套做法沉淀成一个 skill 放进插槽"。
