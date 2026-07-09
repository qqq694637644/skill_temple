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

The API is intentionally allowed to make breaking changes during development. It
does not hide generic runtime failures behind compatibility fallbacks; unexpected
failures should be visible in tests and during endpoint calls.

## Why this exists

Custom GPT Knowledge is useful as a reference source, but it is not a deterministic skill filesystem. For multiple skills, a GPT Action gateway gives you:

- skill resolution by user task and explicit hints such as `@idapython`
- compact manifest summaries and policy rules
- local documentation search with bounded retrieval budgets
- precise safe-path file reading
- version/hash metadata for auditability
- a small OpenAPI surface that is easier for GPT-5.5 to use reliably
- decision-packet fields such as answer readiness, response contract, evidence,
  rank features, and stop conditions

## API surface

The default GPT Action-facing OpenAPI schema is intentionally small:

| Operation | Method | Path | Purpose |
| --- | --- | --- | --- |
| `retrieveSkillContext` | `POST` | `/v1/skills/retrieve` | Default first call for tasks that may require a reusable skill. |
| `searchSkillDocs` | `POST` | `/v1/skills/search` | Targeted follow-up keyword search inside one skill. |
| `readSkillContent` | `POST` | `/v1/skills/read` | Precise safe-path file read. |

Debug endpoints remain callable but are hidden from OpenAPI by default so GPT Actions do not treat them as normal task tools:

| Operation | Method | Path | Purpose |
| --- | --- | --- | --- |
| `listSkills` | `GET` | `/v1/skills` | Setup/debugging. |
| `resolveSkill` | `POST` | `/v1/skills/resolve` | Routing diagnostics; `retrieveSkillContext` already resolves internally. |

## Search behavior

`searchSkillDocs` currently supports only `mode="keyword"`. The keyword engine uses SQLite FTS5 over section-level chunks and boosts exact symbols such as `ida_hexrays.decompile`, `ctree_visitor_t`, `idautils.XrefsTo`, constants, headings, path/module hints, tags, and document priority.

`semantic` and `hybrid` modes are intentionally not exposed until embedding support is added. Skill documentation depends heavily on exact API names, so keyword + symbol matching is the safer default.

Search results include `rank_features` to explain why a result was selected:

```json
{
  "rank_features": {
    "symbol_matches": ["ctree_visitor_t"],
    "document_symbols": ["ida_hexrays.decompile", "cot_call"],
    "path_matches": ["ida_hexrays"],
    "heading_matches": ["ctree"],
    "doc_priority": 20.0
  },
  "why_relevant": "Matched exact API or symbol names."
}
```

## Decision packet

`retrieveSkillContext` returns a decision packet, not just raw documentation. Key fields:

```json
{
  "selected_skills": [
    {
      "skill_id": "idapython",
      "skill_type": "tool_doc",
      "capability_tags": ["reverse_engineering", "ida_pro"],
      "role": "primary",
      "activation": {"confidence": 0.99, "hinted": true},
      "operating_rules": ["Use modern ida_* modules."],
      "evidence": [
        {
          "path": "docs/ida_hexrays.md",
          "section": "Ctree visitor",
          "why_relevant": "Matched exact API or symbol names."
        }
      ],
      "response_contract": {
        "expected_output": "IDAPython code or analysis guidance grounded in the selected docs.",
        "must_include": ["Call ida_auto.auto_wait() before reading analysis results."],
        "evidence_paths": ["docs/ida_hexrays.md"]
      },
      "answer_readiness": {
        "ready": true,
        "recommended_next_action": "answer"
      }
    }
  ],
  "retrieval_budget": {
    "used_docs": 3,
    "used_chars": 4820,
    "truncated": false
  },
  "stop_condition": {
    "satisfied": true,
    "reason": "Selected skill context is sufficient to answer."
  }
}
```

By default, retrieval returns one primary skill. Set `allow_skill_chaining=true`
and increase `max_skills` to return secondary supporting skills.

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
  "skill_type": "tool_doc",
  "capability_tags": ["reverse_engineering", "python_scripting", "ida_pro"],
  "domains": ["binary_analysis"],
  "conflicts_with": ["ghidra", "binary_ninja"],
  "can_chain_with": ["malware_analysis", "yara"],
  "expected_output": "IDAPython code or analysis guidance grounded in the selected docs.",
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
  -Body '{"skill_id":"idapython","query":"ctree visitor calls","mode":"keyword"}'
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

## Retrieval evals

Skill Temple includes a tiny deterministic eval runner for retrieval quality:

```powershell
skill-temple-eval evals/skill_queries.jsonl
```

Each JSONL case can assert expected skill, retrieved docs, and surfaced symbols:

```json
{"query":"@idapython walk ctree calls","expected_skill":"idapython","expected_paths":["docs/ida_hexrays.md"],"expected_symbols":["ctree_visitor_t"]}
```

The eval runner exits non-zero on failures so it can be used in CI later.

## Error behavior

Known input errors return structured details. Unexpected runtime failures are not
wrapped because this project is still in active development.

```json
{
  "detail": {
    "error": {
      "code": "skill_not_found",
      "message": "Skill not found: missing",
      "suggested_next_action": "check_skill_id"
    }
  }
}
```

## Tests

```powershell
py -3 -m pytest
```
