---
name: idapython
description: IDA Pro Python scripting for reverse engineering. Use when writing IDAPython scripts, analyzing binaries, working with IDA APIs for disassembly, decompilation, type systems, cross-references, functions, segments, or database manipulation.
---

# IDAPython

Use modern `ida_*` modules. Avoid legacy `idc` except for compatibility gaps.

## Module Router

| Task | Module | Key Items |
|------|--------|-----------|
| Bytes/memory | `ida_bytes` | `get_bytes`, `patch_bytes`, `get_flags`, `create_*` |
| Functions | `ida_funcs` | `func_t`, `get_func`, `add_func`, `get_func_name` |
| Names | `ida_name` | `set_name`, `get_name`, `demangle_name` |
| Types | `ida_typeinf` | `tinfo_t`, `apply_tinfo`, `parse_decl` |
| Decompiler | `ida_hexrays` | `decompile`, `cfunc_t`, `lvar_t`, ctree visitor |
| Xrefs | `ida_xref` / `idautils` | `XrefsTo`, `XrefsFrom`, `xrefblk_t` |
| Instructions | `ida_ua` | `insn_t`, `op_t`, `decode_insn` |
| Iteration | `idautils` | `Functions()`, `Heads()`, `FuncItems()`, `Strings()` |
| Analysis | `ida_auto` | `auto_wait`, `plan_and_wait` |

## Core Patterns

### Wait for analysis

```python
import ida_auto
ida_auto.auto_wait()
```

### Iterate functions

```python
import idautils
import ida_funcs

for ea in idautils.Functions():
    func = ida_funcs.get_func(ea)
    name = ida_funcs.get_func_name(ea)
```

### Cross-references

```python
import idautils

for xref in idautils.XrefsTo(target_ea):
    print(f"{xref.frm:#x} -> {xref.to:#x} type={xref.type}")
```

## Critical Rules

1. **Use modern modules**: prefer `ida_*` modules and `idautils` over legacy `idc`.
2. **Wait for analysis**: call `ida_auto.auto_wait()` before reading analysis results.
3. **64-bit addresses**: assume `ea_t` can be 64-bit and print addresses with `{ea:#x}`.
4. **Prefer names/xrefs over hardcoded addresses** when generating reusable scripts.
5. **Mutations need review**: preview changes before applying them to a GUI database.

## Anti-Patterns

| Avoid | Do Instead |
|-------|------------|
| `idc.*` for everything | Use modern `ida_*` APIs |
| Hardcoded addresses | Resolve by name, pattern, xref, or function context |
| Reading before auto-analysis completes | Call `ida_auto.auto_wait()` |
| Applying mutations blindly | Use dry-run / preview first |

## Detailed API Reference

Read `docs/<module>.md` for focused docs. This example package includes only a small subset; production deployments should replace it with the full skill documentation tree.
