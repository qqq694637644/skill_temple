---
name: idapython
description: Use for IDAPython scripting, IDA Pro analysis, Hex-Rays decompilation, functions, xrefs, names, types, patches, and IDB automation. 中文：用于 IDA/IDAPython 脚本、Hex-Rays 反编译、伪代码、交叉引用、函数分析、重命名、补丁、类型与 IDB 自动化。
---

# IDAPython example skill

This packaged Skill demonstrates the GPT Actions progressive-disclosure contract. It is an example only; replace it with the Skills required by your project.

## Workflow

1. Read this `SKILL.md` completely.
2. Use the task-specific documentation paths below only when they contribute to the current request.
3. Keep guidance based on documentation separate from facts verified in a live runtime.
4. Do not invent APIs that are absent from the selected references.

## Documentation routing

- For function iteration, strings, and cross-reference helpers, read `docs/idautils.md`.
- For Hex-Rays decompilation, ctree traversal, and local variables, read `docs/ida_hexrays.md`.

## Core guidance

- Prefer modern `ida_*` modules and `idautils` over broad legacy `idc` use.
- Call `ida_auto.auto_wait()` before relying on completed analysis.
- Assume `ea_t` may contain 64-bit addresses.
- Handle missing Hex-Rays support and decompilation failure explicitly.
- Preview mutations and keep them within the user's requested scope.

## Completion

Return the requested guidance or code, identify which references informed it, and state any runtime facts that still require live verification.
