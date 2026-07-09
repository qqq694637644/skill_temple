# Skill Temple

Skill Temple is a small **Skill Runtime gateway** for Custom GPT Actions. It gives a GPT a stable way to retrieve reusable skill instructions and documentation without relying on Custom GPT Knowledge to unpack archives or guess which chunks are relevant.

The intended pattern is:

```text
Custom GPT Instructions
  -> call retrieveSkillContext for skill-backed tasks
  -> call searchSkillDocs or readSkillContent only when more precision is needed
  -> use the returned manifest rules, policy, docs, and validation guidance
```

This repository includes a minimal example `idapython` skill. In production, point `SKILL_TEMPLE_SKILLS_DIR` at your own skills directory.

## Why this exists

Custom GPT Knowledge is useful as a reference source, but it is not a deterministic skill filesystem. For multiple skills, a GPT Action gateway gives you:

- skill resolution by user task and explicit hints such as `@idapython`
- compact manifest summaries and policy rules
- local documentation search with bounded retrieval budgets
- precise safe-path file reading
- version/hash metadata for auditability
- a small OpenAPI surface that is easier for GPT-5.5 to use reliably

## API surface

The GPT Action-facing API is intentionally small:

| Operation | Method | Path | Purpose |
| --- | --- | --- | --- |
| `retrieveSkillContext` | `POST` | `/v1/skills/retrieve` | Default first call for tasks that may require a reusable skill. |
| `searchSkillDocs` | `POST` | `/v1/skills/search` | Targeted follow-up search inside one skill. |
| `readSkillContent` | `POST` | `/v1/skills/read` | Precise safe-path file read. |
| `listSkills` | `GET` | `/v1/skills` | Setup/debugging. |
| `resolveSkill` | `POST` | `/v1/skills/resolve` | Setup/debugging; `retrieveSkillContext` already resolves internally. |

## Skill directory layout

Each skill lives in its own directory:

```text
skills/
  idapython/
    skill.json
    SKILL.md
    INDEX.md
    docs/
      idautils.md
      ida_hexrays.md
```

`SKILL.md` is the model-readable behavior document. `skill.json` is the machine-readable routing, retrieval, and policy metadata.

Minimal `skill.json`:

```json
{
  "skill_id": "idapython",
  "name": "idapython",
  "version": "2026.06.02",
  "description": "IDA Pro Python scripting for reverse engineering.",
  "aliases": ["@idapython", "idapython", "IDA", "Hex-Rays"],
  "entrypoint": "SKILL.md",
  "index": "INDEX.md",
  "activation": {
    "trigger_terms": ["ida_*", "idautils", "ida_hexrays", "decompile", "xrefs"]
  },
  "docs": [
    {"path": "docs/idautils.md", "title": "idautils", "tags": ["iteration", "xrefs"]}
  ],
  "policy": {
    "prefer_structured_reads_first": true,
    "mutations_require_confirmation": true,
    "dry_run_first": true
  }
}
```

## Run locally

```powershell
py -3 -m pip install -e .[dev]
skill-temple --host 127.0.0.1 --port 8765
```

By default, the gateway serves the packaged example skill. To serve your own skills:

```powershell
$env:SKILL_TEMPLE_SKILLS_DIR = "C:\path\to\skills"
skill-temple --host 127.0.0.1 --port 8765
```

or:

```powershell
skill-temple --skills-dir C:\path\to\skills --host 127.0.0.1 --port 8765
```

OpenAPI is available at:

```text
http://127.0.0.1:8765/openapi.json
```

For a public Custom GPT Action, the endpoint must be reachable by OpenAI over HTTPS. A local `127.0.0.1` server is useful for development but not directly reachable by the hosted GPT Action runtime.

## Example requests

Retrieve context for an explicit skill hint:

```powershell
Invoke-RestMethod http://127.0.0.1:8765/v1/skills/retrieve `
  -Method Post `
  -ContentType 'application/json' `
  -Body '{"query":"@idapython write a script to find xrefs to strcpy","hinted_skill_ids":["idapython"]}'
```

Search docs:

```powershell
Invoke-RestMethod http://127.0.0.1:8765/v1/skills/search `
  -Method Post `
  -ContentType 'application/json' `
  -Body '{"skill_id":"idapython","query":"ctree visitor calls"}'
```

Read a specific skill file:

```powershell
Invoke-RestMethod http://127.0.0.1:8765/v1/skills/read `
  -Method Post `
  -ContentType 'application/json' `
  -Body '{"skill_id":"idapython","path":"SKILL.md","start_line":1,"max_lines":80}'
```

## Suggested GPT Instructions

```text
When a user task appears to require a reusable skill, use retrieveSkillContext
with the user's task and any explicit skill hint, such as @idapython.

Use the returned manifest rules as the behavioral source of truth.
Use the returned docs as the initial evidence.
Call searchSkillDocs or readSkillContent only when the retrieved context is
insufficient for the user's concrete request.

Prefer answering with the minimum sufficient skill context.
Stop retrieving once you can satisfy the user's core request with accurate,
task-relevant evidence.

For IDA/IDAPython tasks:
- Prefer structured read-only tools before custom execution.
- Use execute_idapython only when structured tools are insufficient.
- Never apply mutations without explicit user confirmation and dry-run review.
```

## Tests

```powershell
py -3 -m pytest
```
