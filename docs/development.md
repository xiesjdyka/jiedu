# 二次开发

## 开发环境

```powershell
pwsh -NoProfile -File scripts/setup.ps1 -PythonExecutable python
.\.venv312\Scripts\python.exe -m unittest discover -s tests
```

## 目录

```text
knowledge_bridge/   Python核心、MCP、权限、Session与Web API
memory_compiler/    核心背景编译器
importers/          原始数据导入工具
static/             无框架Web Console
tests/              隔离fixture与回归测试源码
docs/               架构和接口文档
examples/           只读客户端示例
```

## 扩展入口

- 新检索策略：从 `core.py` / `context_retrieval.py` 扩展，保持原始证据可追溯。
- 新 MCP 工具：在 `mcp_server.py` 注册，并先接入 `require_interface` 与 `audited`。
- 新客户端：实现 Adapter，传递稳定 `client_id`，不要把业务逻辑写死在 MCP 层。
- 新 Session 来源：写入 `session_manager`，保持 append-only、幂等和 provenance。
- 新前端页面：复用 `static/app.js` 的页面生命周期与状态轮询，不触发索引或模型计算。

## 不变量

- 不把索引当作知识真相源。
- 不让 AI 直接应用正式知识变更。
- 不用摘要替换原始会话。
- 不通过 UI 绕过后端权限。
- 测试数据只能进入临时目录，不能进入真实 Vault、Session 或 `data/`。

## 测试与质量

```powershell
.\.venv312\Scripts\python.exe -m unittest discover -s tests
.\.venv312\Scripts\python.exe -m compileall -q knowledge_bridge
node --check static\app.js
git diff --check
```

涉及 Console 的修改还需在真实浏览器验证。涉及索引的修改必须分别记录 Markdown、Chunk、Vector、Embedding 和 UMAP 是否变化。

## 发布边界

发布包按冻结白名单导出源码、文档、隔离单元测试和模板；不要直接打包工作目录或继承私有 Git 历史。当前开发目录存在未跟踪的有效源码，单用 git archive 也可能漏掉功能。不得包含 `config.json`、`data/`、`logs/`、`backups/`、Vault、`90_原始档案`、模型、密钥或虚拟环境。

## 记忆编译器

`memory_compiler` 仅发布实现与 schema，不发布本机 records 文件。实验性编译入口需要调用者显式提供合成或自己授权的数据 `--records`；仓库不提供作者个人画像记录。

发布导出与 CI 步骤见 [首次发布清单](publishing.md)。不要在个人工作区里运行测试。
