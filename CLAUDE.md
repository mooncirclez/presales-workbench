# 售前工作台 · 内核 (CLAUDE.md)

> 这是一个**可插拔**的售前工程师个人工作台。你(Claude)在本目录工作时:
> **先读 `workbench.json`(唯一事实源)**,再按本文件的规则路由任务。
>
> **运行前提**:macOS + 已登录的 Claude Code CLI。文档提取/通知/导出依赖 macOS 命令。
> **使用者**是售前工程师(本文中的"用户"即指他/她),数据区里是真实客户资料 —— 保密要求见文末。

## 一、设计原则:内核与插件分离
| 区域 | 内容 | 换公司时 |
|---|---|---|
| **框架区**(可复用/可分发) | `CLAUDE.md` `workbench.json` `bin/` `plugins/` | 原样带走 |
| **数据区**(公司专属) | `knowledge/` `customers/` `projects/` `inbox/` `archive/` | 归档或清空 |

项目目标:这套工作台**跨公司复用**,可整体打包分享。所有设计决策向这个目标看齐:**能力进插槽,不要硬编码**。

## 二、四个插槽
### 1. 模板插槽 `plugins/templates/`
- 模板按**包**管理,`workbench.json → templates.active_pack` 指定当前包;包内 `manifest.json` 把**槽位**(`pipeline[].slot`)映射到模板文件。
- 产出交付物时:查当前包的对应槽位 → 用该文件当模板(md/docx/xlsx/pptx 均可)。
- **槽位缺失时,绝不替用户编造公司模板**——先向用户要;用户明确同意后才可给"最小骨架"应急。
- **模板内容由用户上传、用户管版本**。用户给你新模板时:落到包的 `files/` → 更新 manifest(版本 bump)→ 跑 sync。`starter-v1` 是占位包,用户的真实包就位后可整体删除。
- **每个槽位可有多个候选模板**:系统包的 + 用户上传的(存 `plugins/templates/_user/<slot>/<id>.md`,元数据与每槽位默认项在 `_user/index.json` 的 `templates[]` / `defaults{slot:id}`)。上传的 Word/PDF/PPT **自动转 Markdown**(复用 `bin/extract.py` 提取器),Excel 保留原格式;标签自动带「系统默认」/「我的模板」。
- **产出交付物时用哪个模板**:界面「从模板创建」与「AI 生成」都可选;AI 生成的 prompt 会带上所选模板路径并要求**严格参照其章节结构与字段**。系统模板不可删改,可被用户模板顶替为默认。入口在项目页「交付物流水线 → 模板管理 ›」。

### 2. MCP 插槽 `plugins/mcp/`
- 一个外部能力一个 JSON 片段;`mcp.enabled` 决定合并哪些进根目录 `.mcp.json`(**由 sync 生成,勿手改**)。
- **默认不启用任何 MCP**。想接任何知识库(Dify / 飞书 / 自建 RAG 等):加一个片段 + 加回 `enabled` + sync 即可。
- 凭证一律在各服务自己的 `.env`(已 gitignore),不进片段、不进本文件。

### 3. Skill 插槽 `plugins/skills/` + 专家角色 `plugins/roles.json`
- 用户自有技能放 `plugins/skills/<name>/SKILL.md`(**目录名即技能唯一标识**,小写短横线),创建/编辑后整目录自动镜像到 `.claude/skills/` 生效;随仓库分发。工作台「技能」页可增删改、**导入第三方技能**(zip/单 md/文件夹,/api/skill-import)、**自然语言 AI 生成指引**(生成的 SKILL.md 须同时写 plugins 与 .claude 两侧)。
- 替用户生成技能时:参照 meeting-minutes 的结构(何时使用/输入/步骤/输出规范/铁律),产出物路径必须落在数据区规范目录。
- **技能市场**:数据源为一个含 `const DATA = {…}` 的榜单 HTML(路径见 `workbench.json → skill_market.source`)。勾选导入 = 抓 GitHub 原仓库 SKILL.md → 存 `plugins/skills/<id>/`(**本地化,绝不装到全局 `~/.claude/skills/`**)→ 自动加"本地化说明"头。**导入的第三方技能若提到外部 MCP/CLI 工具,不要假装调用**,改用本工作台既有能力(本地文件、bin/ 脚本、已装 MCP);抓取失败时降级为榜单元数据精简版并注明。
- **专家角色** = 人设 + 一组工作台技能,存 `plugins/roles.json`(`{roles:[{id,name,persona,skills:[]}]}`)。AI 任务页选角色后,指令自动携带角色设定与技能清单;执行时**先读取所绑技能的 SKILL.md 再干活**。
- 会话级技能(wecomcli-* / huashu-design / docx / pptx / xlsx / pdf / dataviz)只在桌面对话可用,**不随仓库走**,CLI 后台任务用不到——写文档/设计角色时不要依赖它们。
- 用户说"把这套做法沉淀成 skill"时:固化到 `plugins/skills/`,并问是否要挂进某个角色。

### 3a. 读交付物:二进制文件必须走提取文本(重要)
你的 Read 工具**读不了 docx / xlsx / pptx**(二进制),遇到这些文件:
1. **先找同目录的 `.extracted/<同名>.md`** —— 工作台在上传/生成时已自动提取好,直接读它。
2. 没有伴生文本时,用 `python3 -c` 调 python-docx / openpyxl / python-pptx 提取,或 `pdftotext`(PDF)、`textutil`(doc/rtf/html)。**注意 textutil 对部分 docx 会返回空**,失败要换 python-docx 再试。
3. **两条路都失败就如实说读不出来**,绝对不许根据文件名猜测内容——文件名与实际内容不符是常态(实例:名为「客户提供的POC验收标准.docx」的文件,内容其实是某厂商的解决方案文档)。

同理适用于 `knowledge/`:原文在 `my local knowledge/`,可读文本在 `knowledge/.extracted/`(用 grep 定位后再读)。

### 4. 知识库(纯本地,极简)
- **用户的知识 = `knowledge/my local knowledge/`**:用户自由放文件、自建任意层级子文件夹,工作台不预设分类、不做任何对接。
- `knowledge/reports/` 存周报(生成物)。
- 查资料顺序:该客户/项目目录 → `knowledge/my local knowledge/`。不要建议用户"同步到 ima"或恢复预设分类目录。

## 三、启动与同步
```bash
python3 bin/server.py            # ★ 启动工作台界面 http://127.0.0.1:8917(启动时自动同步插件)
python3 bin/workbench.py sync    # 仅同步插件:重建 .mcp.json、.claude/skills/
python3 bin/workbench.py status  # 打印当前插槽状态(JSON)
```
工作台界面(bin/server.py + bin/app.html)提供:客户/项目/纪要管理、槽位实例化、全文搜索、AI 任务(后台调用本机 `claude -p`,命令模板在 workbench.json 的 `ai.command`,可插拔)。界面里的 AI 任务与你在 Claude Code 里直接对话是同一套规范——都以本文件为准。

**模型与供应商插槽**(界面「AI 任务 → 模型与供应商」):
- 模型 = `ai.model`(留空跟随 CLI 默认,可填 opus/sonnet/fable/haiku),经命令模板的 `{model}` 下发为 `--model`。
- 供应商 = 给 CLI 注入 Anthropic 兼容协议的环境变量(`ANTHROPIC_BASE_URL`/`ANTHROPIC_AUTH_TOKEN`/`ANTHROPIC_MODEL`),**不自研 API 适配器**,以保住读写文件/Skills/多轮等全部 agent 能力。走第三方时 opus 这类别名无效,`ai.model` 被忽略,模型由供应商配置的模型名决定。
- **凭证只落 `.workbench/providers.json`(权限 600、已 gitignore)**:不许写进 workbench.json、不许写进任何可分发文件、不许回传明文给前端(只回脱敏值)。用户让你"帮忙填 key"时,让他自己在界面里填。


## 四、目录与命名(数据区)
- 一客户一档:`customers/<客户简称>/`(结构化档案 `customer.json` + 叙事档案 profile.md)。
- 一商机一档:`projects/<客户简称>-<项目名>/`(元数据 `project.json`),交付物按槽位名前缀(01~07)落盘,版本 `_v1/_v2/_终稿`。
- **纪要与项目绑定**:`projects/<项目>/meetings/YYYY-MM-DD-主题.md`(归档项目的纪要随项目留在 `archive/<项目>/meetings/`)。客户页聚合展示其名下所有项目的纪要;项目页可进「纪要瀑布流」(`#/timeline/<项目>`,最新在最上)。`customers/<id>/meetings/` 是历史遗留位置,新纪要一律不要写那里;遇到遗留纪要可用「纪要」页的「归属项目」迁移。
- 周报在 `knowledge/reports/周报-YYYY-MM-DD.md`;`inbox/` 随手素材整理后清空;结项(赢单/丢单)整目录移入 `archive/`,**合同金额仍计入累计签约统计**。

### 结构化数据 Schema(读写时保持 JSON 合法、字段名不变)
`customers/<id>/customer.json`:
```json
{ "full_name": "", "industry": "", "sales": "对接销售姓名", "level": "战略|重点|一般", "address": "", "website": "",
  "org": "缩进文本表示层级", 
  "contacts": [{ "name": "", "title": "", "role": "决策人|技术把关|使用方|内线|其他", "phone": "", "wechat": "", "note": "态度/影响力" }],
  "kpis": [{ "name": "", "target": "", "current": "", "note": "与我方方案的关联" }], "updated": "" }
```
`projects/<dir>/project.json`:
```json
{ "name": "", "customer": "", "stage": "调研|方案|POC|商务|投标|签约|赢单|丢单",
  "created": "", "budget_wan": 0, "contract_wan": 0, "sign_date": "" }
```
`collab/collab.json`(协作动作,「协作」页管理):
```json
{ "items": [{ "id": "", "project": "必填,绑定 projects/ 目录名", "track": "sales|rd|delivery",
  "type": "销售:项目策略对齐/销售前置动作/售前后续动作;产研:需求单跟进/需求人天评估;交付:SOW 交接/阶段性汇报/案例整理",
  "title": "", "owner_us": "", "owner_peer": "", "due": "", "status": "待启动|进行中|受阻|已完成",
  "mandays": "仅需求人天评估", "note": "" }] }
```
金额单位一律**万元**。看板与周报的统计(商机总预算、累计签约、风险提示)都从这两个文件取数——更新客户/项目信息时**务必同步维护**,不要只写叙事文档。

**统计口径只在后端一处**:`bin/server.py` 的 `compute_dashboard()` 计算全部看板指标与风险规则,经 `/api/state → dashboard` 下发;前端只做展示与跳转,**不要在 app.html 里重算任何统计**。改指标/加风险规则请改 `compute_dashboard()`。

## 五、任务路由(SOP)
| 用户说 | 你做 |
|---|---|
| 建客户档案 | 用当前包 `customer-profile` 槽模板;可用 `wecomcli-contact` 补通讯录信息 |
| 整理会议纪要 | 输入(转写/企微记录)→ 结构化纪要入 `customers/<客户>/meetings/`;行动项经确认后可同步企微待办 |
| 写需求/立项/POC/SOW/投标/合同评审 | 查本地知识库 + 客户档案(customer.json + profile.md)→ 套当前包对应槽位模板 → 产出到项目目录 |
| 毛利测算 | 用 `04-margin` 槽模板(xlsx),只改输入区 |
| 客户汇报/演示 | huashu-design 出高保真页面;图表走 dataviz |
| 约会议/排日程 | wecomcli-meeting / wecomcli-schedule |
| 上传/更新模板 | 按"模板插槽"流程注册 + sync |

## 六、产出规范
- 金额、数据、客户事实**必须可核对,绝不编造**;缺信息留 `【待补充】`。
- 对外承诺(范围/SLA/价格)标注"待商务确认"。
- 正式交付按用户要求的格式渲染(docx/pptx/xlsx/pdf);过程稿用 Markdown。

## 七、保密红线
- 客户信息、报价、合同是敏感数据:**默认只在本地处理**;未经用户明确同意,不发外部服务、不发消息给他人、不对外发布。
- 分享工作台给同事 = 只分享**框架区**;数据区绝不进分发包。任何凭证不落入可分发文件。
