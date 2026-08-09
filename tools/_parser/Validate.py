"""The slice of JSON Schema this repo actually writes, and nothing else.

Every InputSchema.json here uses the same handful of keywords -- type,
properties, required, enum, items, default -- so a dependency that implements
the other ninety percent of the spec would be paying rent on rooms nobody
enters.

Two callers, and they want different things. dispatch() validates a model's
tool arguments before a handler ever sees them, and must not invent values the
handler did not ask for, so it leaves defaults alone. A subagent's structured
output is a return value the caller is about to index into, so there defaults
are filled and the dict comes back complete.

Errors are written to be read by a model, because that is exactly where they
go: a failed submit_result hands this text back as the tool result, and the
next round is the model fixing it. "missing required field 'confidence'" gets
corrected; "$.confidence: required" gets argued with.
"""
import json

def _typename(v) -> str:
    if v is None: return "null"
    if isinstance(v, bool): return "boolean"          # before int: bool is an int
    if isinstance(v, int): return "integer"
    if isinstance(v, float): return "number"
    return {str: "string", dict: "object", list: "array"}.get(type(v), type(v).__name__)

def _is(v, t: str) -> bool:
    if t == "string":  return isinstance(v, str)
    # bool is a subclass of int, so `isinstance(True, int)` is True and a
    # boolean would satisfy an integer field without these guards.
    if t == "integer": return isinstance(v, int) and not isinstance(v, bool)
    if t == "number":  return isinstance(v, (int, float)) and not isinstance(v, bool)
    if t == "boolean": return isinstance(v, bool)
    if t == "object":  return isinstance(v, dict)
    if t == "array":   return isinstance(v, list)
    if t == "null":    return v is None
    return True                                       # unknown keyword: not our business

_TRUE, _FALSE = ("true", "yes", "1"), ("false", "no", "0")

def _coerce(v, t: str):
    """A quoted scalar into the scalar it obviously is.

    Models quote numbers and booleans constantly, and some servers hand back
    tool arguments where every leaf is a string. Rejecting that costs a whole
    round to be told what the value already said. Anything that does not
    convert cleanly is returned untouched, so the type check below still
    reports it.
    """
    if not isinstance(v, str) or t in (None, "string"): return v
    s = v.strip()
    if t == "boolean":
        if s.lower() in _TRUE: return True
        if s.lower() in _FALSE: return False
        return v
    if t in ("number", "integer"):
        try: n = float(s)
        except ValueError: return v
        if t == "integer": return int(n) if n.is_integer() else v
        return n
    if t in ("object", "array"):
        try: parsed = json.loads(s)
        except (json.JSONDecodeError, ValueError): return v
        return parsed if _is(parsed, t) else v
    if t == "null" and s.lower() in ("null", "none", ""): return None
    return v

def _label(path: str) -> str: return f"'{path}'" if path else "input"

def check(value, schema: dict, path: str = "", defaults: bool = False) -> tuple:
    """(cleaned value, list of errors). Never raises -- a malformed schema
    yields no errors rather than an exception in the middle of a turn.

    Unknown keys are kept, not stripped: a handler that reads something its
    schema forgot to declare keeps working, which is the friendlier failure of
    the two.
    """
    schema = schema if isinstance(schema, dict) else {}
    t = schema.get("type")
    value = _coerce(value, t)
    if t and not _is(value, t):
        return value, [f"{_label(path)}: expected {t}, got {_typename(value)}"]
    enum = schema.get("enum")
    if enum is not None and value not in enum:
        return value, [f"{_label(path)}: must be one of {enum}, got {value!r}"]

    props = schema.get("properties")
    if isinstance(value, dict) and (props is not None or t == "object"):
        errors, out = [], dict(value)
        for k, sub in (props or {}).items():
            if k in value:
                out[k], errs = check(value[k], sub, f"{path}.{k}" if path else k, defaults)
                errors += errs
            elif defaults and isinstance(sub, dict) and "default" in sub:
                out[k] = sub["default"]
        for k in schema.get("required") or []:
            if k not in value:
                errors.append(f"missing required field '{f'{path}.{k}' if path else k}'")
        return out, errors

    items = schema.get("items")
    if isinstance(value, list) and items:
        errors, out = errors_of(value, schema, path), []
        for i, item in enumerate(value):
            v, errs = check(item, items, f"{path}[{i}]", defaults)
            out.append(v)
            errors += errs
        return out, errors

    return value, errors_of(value, schema, path)

def errors_of(value, schema: dict, path: str = "") -> list:
    """Leaf-level keywords that survive the recursion above."""
    out = []
    if isinstance(value, list):
        n = len(value)
        lo, hi = schema.get("minItems"), schema.get("maxItems")
        if lo is not None and n < lo: out.append(f"{_label(path)}: expected at least {lo} items, got {n}")
        if hi is not None and n > hi: out.append(f"{_label(path)}: expected at most {hi} items, got {n}")
    return out

def validate(value, schema: dict) -> list:
    """Errors only, for callers that do not want the cleaned value."""
    return check(value, schema)[1]
