# 模板插槽(Template Packs)

模板按**包(pack)**管理,一个公司一个包,可整包替换、独立升级版本。**模板内容由你上传维护,Claude 不代写公司模板**。

## 包的结构
```text
plugins/templates/
├── README.md            # 本文件
├── starter-v1/          # 占位起步包(可删)
│   ├── manifest.json    # 包清单:id / version / 槽位→文件映射
│   └── files/           # 模板文件本体(md / docx / xlsx / pptx 都可以)
└── <你的公司包>/         # 例:acme-2026/
    ├── manifest.json
    └── files/
```

## manifest.json 格式
```json
{
  "id": "acme-2026",
  "version": "1.0.0",
  "label": "Acme 公司售前模板包",
  "origin": "user-upload",
  "updated": "2026-08-01",
  "files": {
    "02-project-approval": "files/立项报告模板.docx",
    "04-margin": "files/毛利测算-财务版.xlsx"
  }
}
```
- `files` 的 key 是**槽位名**(见 `workbench.json` 的 `pipeline[].slot`),value 是包内相对路径。
- **不必填满所有槽位**:缺的槽位控制台会标"未挂载",Claude 遇到会先问你要模板,不会擅自编。
- 文件格式随意:md / docx / xlsx / pptx 均可,Claude 会按格式读写。

## 常用操作
| 操作 | 做法 |
|---|---|
| 上传公司模板 | 建 `<包名>/files/` 放文件 → 写 manifest.json → 改 `workbench.json` 的 `active_pack` → `python3 bin/workbench.py sync` |
| 更新某个模板 | 覆盖 files/ 里的文件 → manifest 的 `version` 手动 bump(如 1.0.0 → 1.1.0)→ sync |
| 换公司 | 新建新包 + 切 `active_pack`;旧包保留即是历史归档 |
| 删除占位包 | `rm -rf plugins/templates/starter-v1`(先确认 active_pack 已指向新包) |

也可以直接对 Claude 说:"把这个文件注册为 05-sow 模板"——Claude 会替你落文件、改 manifest、跑 sync。
