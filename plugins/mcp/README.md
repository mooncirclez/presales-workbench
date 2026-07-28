# MCP 插槽

一个外部能力一个 JSON 片段,文件名即插件 id:

```json
// plugins/mcp/my-kb.json
{
  "mcpServers": {
    "my-kb": { "type": "http", "url": "http://127.0.0.1:8081/mcp" }
  }
}
```

启用:把 id 加进 `workbench.json → mcp.enabled`,然后

```bash
python3 bin/workbench.py sync   # 合并生成根目录 .mcp.json
```

`.mcp.json` 是生成物(已 gitignore),不要手改。

## 注意

- **凭证只放各服务自己的 `.env`**,不要写进片段文件 —— 片段会随仓库分发
- 默认不启用任何 MCP;工作台的核心功能不依赖 MCP
