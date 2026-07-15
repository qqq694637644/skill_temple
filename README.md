# Skill Temple

Skill Temple is a reusable **Codex-style Skill runtime adapted to Custom GPT Actions**.
It provides a small OpenAPI surface for projects that want filesystem-based `SKILL.md`
instructions, model-driven Skill selection, and progressive disclosure without copying
all domain documentation into the Custom GPT Instructions field.

The runtime was synchronized from the generic Skill layer used by `ida_skill`. It does
not include IDA Actions or any project-specific backend operations.

## What this template provides

- `SKILL.md` is the only required Skill entrypoint.
- Discovery uses only frontmatter `name` and `description`.
- The model chooses Skills; the server does not perform semantic keyword ranking.
- Codex-style `$skill-name` mentions are supported.
- `@skill-name` is also supported as a gateway convenience extension.
- Selected `SKILL.md` files are returned within a shared response budget.
- Referenced resources are read progressively by safe relative path.
- Keyword search is available as a fallback inside one selected Skill.
- Multiple explicit Skills are loaded together up to a maximum of three.
- The discovery catalog has a separate 20,000-character budget.
- Optional Bearer authentication is supported for `/v1/*` routes.
- Nine operations are exposed: three Skill actions and six generic Workspace actions.

## Public GPT Actions

| operationId | Method | Path | Purpose |
| --- | --- | --- | --- |
| `retrieveSkillContext` | `POST` | `/v1/skills/retrieve` | Return a bounded Skill catalog or load explicitly selected `SKILL.md` files. |
| `readSkillContent` | `POST` | `/v1/skills/read` | Read an exact safe relative path with continuation metadata. |
| `searchSkillDocs` | `POST` | `/v1/skills/search` | Search indexed resources inside one selected Skill. |
| `workspaceCommand` | `POST` | `/v1/workspace/command` | Start or manage an asynchronous PowerShell command under `WORKSPACE_ROOT`. |
| `workspaceInspect` | `POST` | `/v1/workspace/inspect` | Inspect the workspace tree, search matches, and bounded file snippets. |
| `workspaceSearch` | `POST` | `/v1/workspace/search` | Search selected workspace paths with ripgrep. |
| `workspaceReadFiles` | `POST` | `/v1/workspace/read-files` | Read multiple UTF-8 files with line numbers and continuation metadata. |
| `workspaceWriteFile` | `POST` | `/v1/workspace/write-file` | Create or replace one UTF-8 text file with hash and dry-run controls. |
| `workspaceApplyPatch` | `POST` | `/v1/workspace/apply-patch` | Apply a controlled multi-file text patch with rollback on failure. |

All public operations publish:

```json
{"x-openai-isConsequential": false}
```

Projects can add their own domain Actions beside these built-in operations.

## Workspace Actions

Workspace Actions provide reusable local repository and project-file operations without
depending on any domain application. All relative paths are resolved from
`WORKSPACE_ROOT`.

- `workspaceInspect` combines a bounded directory tree, optional ripgrep searches, and
  related UTF-8 file snippets.
- `workspaceSearch` supports literal or regular-expression matching, case controls,
  context lines, and response-size limits.
- `workspaceReadFiles` returns numbered lines, SHA-256 metadata, truncation state, and
  `next_start_line` continuation.
- `workspaceWriteFile` supports create-only, unconditional overwrite, and hash-checked
  overwrite modes, plus line-ending selection and dry-run.
- `workspaceApplyPatch` supports Add, Update, and optionally Delete operations. Changes
  are staged and rolled back when a commit step fails.
- `workspaceCommand` runs PowerShell 7 asynchronously. Use `start`, then poll `get` or
  read incremental `logs` until a terminal state is reached; `cancel` terminates the
  process tree.

Text mutations use `.skill-temple-workspace-transactions` beside the workspace while a
transaction is active. Successful transactions clean this directory automatically.
Workspace search requires `rg`; command execution requires `pwsh`.

## Skill directory contract

A Skill requires one entrypoint:

```text
skills/
  example-skill/
    SKILL.md
    docs/
      reference.md
    scripts/
      helper.py
    assets/
      template.txt
```

`SKILL.md` must begin with YAML frontmatter:

```yaml
---
name: example-skill
description: Use for tasks that require the example domain. 中文：用于示例领域任务。
---
```

Rules:

- `name` becomes the stable `skill_id` selection handle.
- `name` is limited to 64 characters and must match the Skill ID format.
- `description` is required and limited to 1,024 characters.
- Use a bilingual description when the GPT serves more than one language.
- Do not add `skill.json` or `INDEX.md`; the runtime ignores them.
- Put task routing and behavior in `SKILL.md`.
- Put detailed reference material in `docs/`, `references/`, `scripts/`, or `assets/`.
- Point to exact relative paths from `SKILL.md` when the model should read them.

Example:

```markdown
---
name: api-review
description: Use for reviewing API schemas, compatibility, and versioning risks.
---

# API review

1. Read this file completely.
2. For OpenAPI compatibility, read `docs/openapi.md`.
3. For migration rules, read `docs/versioning.md`.
4. Return findings, evidence, and unverified risks separately.
```

## Selection flow

Custom GPT Instructions cannot receive a dynamic server catalog before the model turn,
so this template uses a two-call GPT Actions adaptation.

### 1. Discover

Call without explicit hints:

```json
{
  "query": "Review this API migration",
  "hinted_skill_ids": [],
  "allow_skill_chaining": false
}
```

The response contains a bounded `available_skills` catalog:

```json
{
  "selected_skills": [],
  "available_skills": [
    {
      "skill_id": "api-review",
      "name": "api-review",
      "description": "Use for reviewing API schemas...",
      "description_truncated": false,
      "entrypoint": "SKILL.md",
      "content_hash": "sha256:..."
    }
  ],
  "available_skill_count": 1,
  "included_skill_count": 1,
  "omitted_skill_count": 0,
  "descriptions_truncated": false,
  "catalog_char_limit": 20000,
  "catalog_included": true,
  "decision": {
    "selected": false,
    "next_action": "selectSkillOrAnswer",
    "stop_retrieval": false
  }
}
```

The model reviews the visible `name` and `description`. The server does not score the
query against descriptions.

When `omitted_skill_count > 0` or `descriptions_truncated=true`, the visible catalog is
not the complete installed set.

### 2. Load an exact Skill

When one description clearly applies, retry once with the exact selection handle:

```json
{
  "query": "Review this API migration",
  "hinted_skill_ids": ["api-review"],
  "allow_skill_chaining": false
}
```

A selected packet contains:

```json
{
  "skill_id": "api-review",
  "name": "api-review",
  "description": "Use for reviewing API schemas...",
  "role": "primary",
  "source_path": "SKILL.md",
  "instructions": "---\nname: api-review\n...",
  "content_hash": "sha256:...",
  "total_lines": 42,
  "truncated": false,
  "next_start_line": null,
  "referenced_paths": ["docs/openapi.md", "docs/versioning.md"]
}
```

Selected responses omit the repeated catalog:

```json
{
  "available_skills": [],
  "catalog_included": false
}
```

## Explicit mentions

Codex-style mention:

```text
$api-review
```

Gateway extension:

```text
@api-review
```

Unknown textual mentions are returned in:

```json
{"unknown_skill_mentions": ["missing-skill"]}
```

Unknown `hinted_skill_ids` return the existing structured HTTP 404 because hints are
strict selection handles supplied by the caller.

## Multiple Skills

Two or three exact hints or mentions load automatically together. The caller does not
need to set `allow_skill_chaining=true`; that field remains only for backward
compatibility.

```json
{
  "query": "Use $api-review and $release-notes",
  "hinted_skill_ids": []
}
```

Packets receive `primary` and `secondary` roles in explicit selection order.

More than three explicit selections are never partially executed. The runtime returns:

```json
{
  "selected_skills": [],
  "explicit_skill_ids": ["one", "two", "three", "four"],
  "omitted_explicit_skill_ids": ["four"],
  "decision": {
    "selected": false,
    "next_action": "retryWithFewerSkills"
  }
}
```

## Response budgets

- Catalog budget: 20,000 serialized JSON characters.
- Single `SKILL.md` budget: 24,000 characters.
- Combined selected instructions budget: 60,000 characters.
- Maximum selected Skills per call: 3.

When a selected entrypoint is truncated, the packet returns `next_start_line`. Continue
with `readSkillContent`; do not call `retrieveSkillContext` again just to continue the
same file.

## Reading referenced resources

```json
{
  "skill_id": "api-review",
  "path": "docs/openapi.md",
  "start_line": 1,
  "max_lines": 300
}
```

The response includes:

```text
start_line
end_line
total_lines
content
content_hash
truncated
next_start_line
```

Paths are constrained to the selected Skill root. Absolute paths and traversal such as
`../README.md` are rejected.

A single line longer than the normal character budget is returned intact rather than
silently losing the remainder of that line.

## Search fallback

Use `searchSkillDocs` only when the selected `SKILL.md` does not identify an exact
resource path:

```json
{
  "skill_id": "api-review",
  "query": "breaking change schema compatibility",
  "paths": null,
  "limit": 5
}
```

Search uses SQLite FTS5 over section-level chunks and boosts exact symbols, path terms,
and headings. Search is scoped to one explicit `skill_id`.

## Configuration

Copy `.env.example` to `.env`:

```dotenv
SKILL_TEMPLE_SERVER_URL=https://skills.example.com
SKILL_TEMPLE_SKILLS_DIR=C:/path/to/project/skills
SKILL_TEMPLE_BEARER_TOKEN=replace-with-a-long-random-secret
WORKSPACE_ROOT=C:/path/to/project/workspace
WORKSPACE_PWSH_PATH=pwsh
WORKSPACE_OPERATION_ROOT=C:/path/to/project/.runtime/workspace-operations
WORKSPACE_ALLOW_NETWORK=false
WORKSPACE_COMMAND_TIMEOUT_SECONDS=120
WORKSPACE_COMMAND_MAX_TIMEOUT_SECONDS=3600
WORKSPACE_COMMAND_OUTPUT_BYTES=1000000
WORKSPACE_COMMAND_MAX_OUTPUT_BYTES=10000000
```

`SKILL_TEMPLE_SKILLS_DIR` lookup order:

1. explicit `create_app(skills_dir=...)` or `SkillRuntime(path)` argument;
2. environment variable;
3. `.env` in the current working directory;
4. local `./skills` directory;
5. packaged example Skills.

When `SKILL_TEMPLE_BEARER_TOKEN` is set, `/v1/*` and `/console/retrieve` require:

```text
Authorization: Bearer <token>
```

`/openapi.json`, `/health`, and `/console` remain public so the schema can be imported
and the debug console can load.

`WORKSPACE_ALLOW_NETWORK=false` rejects known download and request commands even when a
caller asks for network access. Workspace commands also reject GitHub CLI authentication,
secret operations, environment enumeration, SSH, and SCP. The subprocess receives a
sanitized environment instead of the gateway process environment.

## Install and run

```powershell
py -3 -m pip install -e .[dev]
skill-temple --host 127.0.0.1 --port 8765
```

With a custom Skill directory:

```powershell
skill-temple --skills-dir C:/path/to/project/skills --host 127.0.0.1 --port 8765
```

OpenAPI:

```text
http://127.0.0.1:8765/openapi.json
```

Health:

```text
http://127.0.0.1:8765/health
```

Debug console:

```text
http://127.0.0.1:8765/console
```

## Add project-specific Actions

Keep the Skill runtime generic and register project-specific Actions in a separate
module:

```python
from fastapi import FastAPI


def register_project_actions(app: FastAPI) -> None:
    @app.post(
        "/v1/project/read",
        operation_id="readProjectData",
        openapi_extra={"x-openai-isConsequential": False},
    )
    def read_project_data(request: ProjectRequest) -> dict[str, object]:
        return {"result": "..."}
```

Then call `register_project_actions(app)` near the end of `create_app()`.

Do not put project-specific tool rules into the global Skill runtime. Put domain behavior
inside the selected `SKILL.md` and keep operationId names aligned with the project's
OpenAPI schema.

## Validation

```powershell
py -3 -m ruff check .
py -3 -m pytest
py -3 -m skill_temple.evals evals/skill_queries.jsonl
```

The test suite covers:

- `SKILL.md`-only discovery;
- exact hints and `$skill` / `@skill` mentions;
- no server-side semantic routing;
- bounded catalog and instruction responses;
- multi-Skill explicit selection;
- unknown mentions;
- continuation and path safety;
- OpenAPI operation/schema stability;
- optional Bearer authentication;
- deterministic search/eval behavior;
- Workspace OpenAPI contracts, UTF-8 reads, search/inspect behavior, and response bounds;
- file write modes, hash checks, dry-run behavior, patch rollback, and cleanup failures;
- asynchronous command success, failure, timeout, cancellation, recovery, and idempotency.

## Packaged example

The repository includes an `idapython` Skill only as a realistic progressive-disclosure
example:

```text
src/skill_temple/example_skills/idapython/
  SKILL.md
  docs/
    idautils.md
    ida_hexrays.md
```

It does not add IDA Actions to this template. Replace it with the Skills for your own
project or point `SKILL_TEMPLE_SKILLS_DIR` at another directory.
