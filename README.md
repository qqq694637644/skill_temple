# Skill Temple

Skill Temple 把 Codex 的 Skill 思路适配到 Custom GPT Actions：

1. 构建时把每个 Skill 的 `name + description + skill_id` 编译进 GPT Instructions。
2. 模型在初始上下文中看到 Skill 目录，自行选择需要的 Skill。
3. 选中后调用 `loadSkills`，只加载对应的完整 `SKILL.md`。
4. `SKILL.md` 引用的其他文件再通过 `readSkillContent` 按需读取。
5. 实际项目读取、修改和命令执行由 Workspace Actions 完成。

不会把所有 Skill 正文静态塞进 prompt，也不需要先调用 Action 查询目录。

## 公开 Actions

| operationId | 路径 | 用途 |
| --- | --- | --- |
| `loadSkills` | `POST /v1/skills/load` | 按精确 `skill_id` 加载完整 `SKILL.md` |
| `readSkillContent` | `POST /v1/skills/read` | 读取选中 Skill 内的引用文件 |
| `workspaceInspect` | `POST /v1/workspace/inspect` | 查看目录、搜索结果和文件片段 |
| `workspaceSearch` | `POST /v1/workspace/search` | 使用 ripgrep 搜索工作区 |
| `workspaceReadFiles` | `POST /v1/workspace/read-files` | 读取工作区文件 |
| `workspaceWriteFile` | `POST /v1/workspace/write-file` | 创建或覆盖文本文件 |
| `workspaceApplyPatch` | `POST /v1/workspace/apply-patch` | 应用多文件文本补丁 |
| `workspaceCommand` | `POST /v1/workspace/command` | 异步运行 PowerShell 7 命令 |

## Skill 目录

```text
skills/
  api-review/
    SKILL.md
    docs/
      openapi.md
    scripts/
      helper.py
```

`SKILL.md` 必须包含 frontmatter：

```markdown
---
name: api-review
description: Review API schemas, compatibility, and migration risks.
---

# API review

Read `docs/openapi.md` when the task involves OpenAPI compatibility.
```

`name` 同时作为稳定的 `skill_id`。详细资料放在 `docs/`、`references/`、`scripts/` 或 `assets/`，并从 `SKILL.md` 中明确引用。

## 生成 GPT Instructions

`GPT_ACTION_PROMPT.md` 是模板，其中包含：

```text
{{SKILL_CATALOG}}
```

安装后运行：

```powershell
skill-temple-build-prompt --skills-dir C:/path/to/skills
```

默认输出：

```text
dist/GPT_INSTRUCTIONS.md
```

生成器会把当前所有 Skill 的元数据替换进模板：

```text
- api-review: Review API schemas, compatibility, and migration risks. (skill_id: api-review)
- release-notes: Draft release notes from repository changes. (skill_id: release-notes)
```

把生成文件复制到 Custom GPT 的 Instructions。Skill 增删或 description 修改后重新生成即可。

也可以指定输入输出：

```powershell
skill-temple-build-prompt `
  --skills-dir C:/path/to/skills `
  --template GPT_ACTION_PROMPT.md `
  --output dist/GPT_INSTRUCTIONS.md
```

## 生成 `openapi.json`

安装后运行：

```powershell
skill-temple-build-openapi
```

默认输出根目录的 `openapi.json`。生成器优先读取 `.env` 中的：

```dotenv
SKILL_TEMPLE_SERVER_URL=https://skills.example.com
SKILL_TEMPLE_OPENAPI_OUTPUT=openapi.json
```

因此生成结果会包含：

```json
{
  "servers": [
    {"url": "https://skills.example.com"}
  ]
}
```

也可以直接覆盖：

```powershell
skill-temple-build-openapi `
  --server-url https://skills.example.com `
  --output openapi.json
```

## `loadSkills`

请求：

```json
{
  "skill_ids": ["api-review"]
}
```

响应中的 `skills[].content` 使用 Codex 风格的上下文块：

```xml
<skill>
<name>api-review</name>
<path>api-review/SKILL.md</path>
完整 SKILL.md 内容
</skill>
```

一次可以加载多个 Skill。运行时只做精确 ID 加载，不替模型判断哪个 Skill 匹配任务。

## `readSkillContent`

```json
{
  "skill_id": "api-review",
  "path": "docs/openapi.md",
  "start_line": 1,
  "max_lines": 300
}
```

相对路径被限制在对应 Skill 目录内。响应包含 `truncated` 和 `next_start_line`，大型引用文件可以继续读取。

## 配置

复制 `.env.example` 为 `.env`：

```dotenv
SKILL_TEMPLE_SERVER_URL=https://skills.example.com
SKILL_TEMPLE_SKILLS_DIR=C:/path/to/project/skills
SKILL_TEMPLE_BEARER_TOKEN=replace-with-a-long-random-secret
SKILL_TEMPLE_OPENAPI_OUTPUT=openapi.json
WORKSPACE_ROOT=C:/path/to/project/workspace
WORKSPACE_PWSH_PATH=pwsh
WORKSPACE_OPERATION_ROOT=C:/path/to/project/.runtime/workspace-operations
WORKSPACE_ALLOW_NETWORK=false
WORKSPACE_COMMAND_TIMEOUT_SECONDS=120
WORKSPACE_COMMAND_MAX_TIMEOUT_SECONDS=3600
WORKSPACE_COMMAND_OUTPUT_BYTES=1000000
WORKSPACE_COMMAND_MAX_OUTPUT_BYTES=10000000
```

Skill 目录查找顺序：

1. 命令行或 `create_app(skills_dir=...)`
2. `SKILL_TEMPLE_SKILLS_DIR`
3. 当前目录 `.env`
4. 当前目录的 `skills/`
5. 包内示例 Skill

设置 `SKILL_TEMPLE_BEARER_TOKEN` 后，所有 `/v1/*` 接口以及控制台的加载、读取请求都要求：

```text
Authorization: Bearer <token>
```

`/openapi.json`、`/health` 和 `/console` 保持公开，方便导入 schema 和打开调试页面。生成的 OpenAPI 会自动包含 `BearerAuth` security scheme。

## 安装和运行

```powershell
py -3 -m pip install -e ".[dev]"
skill-temple --host 127.0.0.1 --port 8765
```

OpenAPI：

```text
http://127.0.0.1:8765/openapi.json
```

健康检查：

```text
http://127.0.0.1:8765/health
```

调试检索控制台：

```text
http://127.0.0.1:8765/console
```

控制台可以查看 Skill 目录、调用 `loadSkills`，以及读取选中 Skill 内的引用文件。Token 只保存在当前浏览器标签页的 `sessionStorage`。

## Skill 检索评测

评测工具验证编译目录、精确加载、引用路径和关键符号是否可达：

```powershell
skill-temple-eval evals/skill_queries.jsonl
```

JSONL 示例：

```json
{"id":"api-review","query":"review API compatibility","expected_skill":"api-review","expected_paths":["docs/openapi.md"],"expected_symbols":["breaking change"]}
```

当前架构由模型根据静态目录选择 Skill，因此该工具不模拟服务端语义路由，只检查被选 Skill 的加载链路和引用资料是否完整。

## 验证

```powershell
python -m ruff check .
python -m pytest -q
skill-temple-eval evals/skill_queries.jsonl
skill-temple-build-openapi --output .runtime/openapi.json
```

测试覆盖：Skill 扫描、目录生成、精确加载、Codex 风格上下文、引用路径发现、安全读取、Bearer Token、调试控制台、评测工具、OpenAPI 生成和 Workspace Actions。
