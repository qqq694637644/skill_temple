# ida_hexrays

Hex-Rays decompiler APIs for pseudocode, ctree traversal, and local variables.

## Decompile a function

Call `ida_hexrays.decompile(ea)` with an address inside a function. It returns a `cfunc_t` when decompilation succeeds.

```python
import ida_auto
import ida_hexrays

ida_auto.auto_wait()
cfunc = ida_hexrays.decompile(func_ea)
if cfunc:
    print(str(cfunc))
```

## Local variables

`cfunc.lvars` contains decompiler local variables. Use `lvar.name` and `lvar.type()` for display.

```python
for lvar in cfunc.lvars:
    print(f"{lvar.name}: {lvar.type()}")
```

## Ctree visitor

Use `ida_hexrays.ctree_visitor_t` when you need to inspect decompiled expressions.

```python
class CallVisitor(ida_hexrays.ctree_visitor_t):
    def visit_expr(self, expr):
        if expr.op == ida_hexrays.cot_call:
            print(f"call at {expr.ea:#x}")
        return 0

cfunc = ida_hexrays.decompile(func_ea)
if cfunc:
    CallVisitor().apply_to(cfunc.body, None)
```

## Fallback

If Hex-Rays is unavailable or decompilation fails, fall back to disassembly and xrefs using `idautils`, `ida_funcs`, and `ida_ua`.
