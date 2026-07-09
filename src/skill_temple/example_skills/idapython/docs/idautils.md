# idautils

Common iterators for IDAPython scripts.

## Functions

`idautils.Functions(start=None, end=None)` iterates function start addresses. Combine it with `ida_funcs.get_func_name(ea)` or `ida_funcs.get_func(ea)`.

```python
import ida_auto
import ida_funcs
import idautils

ida_auto.auto_wait()
for func_ea in idautils.Functions():
    print(f"{func_ea:#x} {ida_funcs.get_func_name(func_ea)}")
```

## FuncItems

`idautils.FuncItems(func_ea)` iterates instruction/data heads in a function body.

```python
for head in idautils.FuncItems(func_ea):
    print(f"{head:#x}")
```

## XrefsTo and XrefsFrom

Use `idautils.XrefsTo(ea)` and `idautils.XrefsFrom(ea)` for convenient xref iteration.

```python
for xref in idautils.XrefsTo(target_ea):
    print(f"caller={xref.frm:#x} callee={xref.to:#x} type={xref.type}")
```

## Strings

`idautils.Strings()` iterates IDA string items. Convert each item with `str(item)` and read its address with `item.ea`.
