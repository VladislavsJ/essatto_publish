"""Recipes: named scripts the planner model can choose (described for the model in ../RECIPES.md).
A recipe does not compute anything by itself: it compiles to an ordinary plan (filters + formula) that execute.run
evaluates, so every recipe answer has the same trace, evidence and trust checks as any other answer.

 compile(plan, store) -> (compiled_plan, None) | (None, "why not")
 dictionary(store)    -> {kind: {attr: {"unit", "meaning"}}}   what the attributes of this drawing mean
 safe_expr(text, names) / eval_expr(tree, row)                  the only arithmetic a plan may ask for"""
import ast, os, operator

HERE = os.path.dirname(__file__)
RECIPES_MD = os.path.join(HERE, "..", "RECIPES.md")

# meaning and unit of the attributes extractors produce. An extractor can add or override entries in store.meta["dictionary"].
BASE = {
    "room": {"area": ("m²", "floor area of the room, wall face to wall face"), "perimeter": ("m", "length of the room outline, door openings included"),
             "door_width": ("m", "total width of the door openings on the room's boundary"), "baseboard": ("m", "perimeter minus door openings"),
             "doors": ("count", "number of doors on the room's boundary"), "windows": ("count", "number of windows on the room's boundary"),
             "name": (None, "room name printed inside the room"), "rtype": (None, "room type recognised from the name"), "building": (None, "which separate building on the sheet"),
             "level": (None, "storey"), "apartment": (None, "dwelling unit"), "height": ("m", "room height"), "volume": ("m³", "room volume")},
    "door": {"width": ("m", "clear width of the opening measured between the wall ends"), "rooms": (None, "names of the rooms the door joins")},
    "window": {"width": ("m", "width of the opening measured between the wall ends"), "rooms": (None, "name of the room the window belongs to")},
}

def dictionary(store):
    out = {}
    have = {}
    for e in store.entities:
        s = have.setdefault(e.kind, set())
        for k, v in e.attrs.items():
            if v is not None: s.add(k)
    for kind, attrs in BASE.items():
        for k, (u, m) in attrs.items():
            if k in have.get(kind, ()): out.setdefault(kind, {})[k] = {"unit": u, "meaning": m}
    for kind, attrs in (store.meta.get("dictionary") or {}).items():
        for k, d in attrs.items(): out.setdefault(kind, {})[k] = d
    return out

def unit_of(store, kind, attr):
    d = dictionary(store).get(kind if isinstance(kind, str) else "", {}).get(attr)
    return d["unit"] if d else None

def recipes_text():
    try: return open(RECIPES_MD, encoding="utf-8").read()
    except Exception: return ""

# ---- formulas ---------------------------------------------------------------------------------------------------
_BIN = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul, ast.Div: operator.truediv}
def safe_expr(text, names):
    """parse a formula; only numbers, known attribute names, + - * / and brackets. Returns (tree, None) or (None, why)."""
    try: tree = ast.parse(str(text), mode="eval")
    except SyntaxError as e: return None, f"formula {text!r} is not valid: {e.msg}"
    for n in ast.walk(tree):
        if isinstance(n, (ast.Expression, ast.BinOp, ast.UnaryOp, ast.USub, ast.UAdd, ast.Load) + tuple(_BIN)): continue
        if isinstance(n, ast.Constant) and isinstance(n.value, (int, float)) and not isinstance(n.value, bool): continue
        if isinstance(n, ast.Name):
            if n.id in names: continue
            return None, f"formula uses {n.id!r}, which is not a numeric attribute of this kind (available: {sorted(names)})"
        return None, f"formula {text!r} uses something other than numbers, attributes and + - * /"
    return tree, None

def eval_expr(tree, row):
    """value of the formula for one entity; None when an attribute it needs is missing on that entity"""
    def ev(n):
        if isinstance(n, ast.Expression): return ev(n.body)
        if isinstance(n, ast.Constant): return n.value
        if isinstance(n, ast.Name):
            v = row.get(n.id)
            if not isinstance(v, (int, float)) or isinstance(v, bool): raise KeyError(n.id)
            return v
        if isinstance(n, ast.UnaryOp): return -ev(n.operand) if isinstance(n.op, ast.USub) else ev(n.operand)
        return _BIN[type(n.op)](ev(n.left), ev(n.right))
    try: return ev(tree)
    except (KeyError, ZeroDivisionError): return None

def numeric_attrs(store, kind):
    out = set()
    for e in store.of(kind):
        for k, v in e.attrs.items():
            if isinstance(v, (int, float)) and not isinstance(v, bool): out.add(k)
    return out

# ---- recipes ----------------------------------------------------------------------------------------------------
def _num(args, key, default=None):
    v = (args or {}).get(key, default)
    try: return float(v) if v is not None else None
    except (TypeError, ValueError): return None

def _waste(expr, args, notes):
    w = _num(args, "waste_percent", 0) or 0
    if w: notes.append(f"{w:g}% waste added"); return f"({expr}) * {1 + w / 100:g}"
    return expr

def _baseboard(p, st, na):
    notes = []
    if "perimeter" not in na: return None, "this drawing has no room outlines, so no perimeter to measure baseboard along"
    if "door_width" in na: expr = "perimeter - door_width"; notes.append("baseboard = room perimeter − width of the door openings on its boundary")
    else: expr = "perimeter"; notes.append("baseboard = room perimeter; door openings are NOT deducted because this drawing's doors are not linked to rooms")
    return {"op": "sum", "kind": "room", "expr": _waste(expr, p.get("args"), notes), "unit": "m", "as": "baseboard", "notes": notes}, None

def _floor_area(p, st, na):
    if "area" not in na: return None, "this drawing has no room areas"
    a = p.get("args") or {}; g = a.get("group_by")
    out = {"op": "group_sum" if g else "sum", "kind": "room", "attr": "area", "group_by": g, "notes": ["floor area = area of the room outline, wall face to wall face"]}
    if a.get("exclude"): out["exclude"] = a["exclude"]
    return out, None

def _wall_area(p, st, na):
    a = p.get("args") or {}; h = _num(a, "height_m")
    if "perimeter" not in na: return None, "this drawing has no room outlines"
    if not h: return None, "wall area needs the ceiling height, which a floor plan does not show. Ask again with the height, e.g. 'wall area of the library at 2.7 m height'"
    notes = [f"wall area = (perimeter − door widths) × height {h:g} m" if "door_width" in na else f"wall area = perimeter × height {h:g} m (door openings not deducted: doors are not linked to rooms)"]
    expr = f"({'perimeter - door_width' if 'door_width' in na else 'perimeter'}) * {h:g}"
    wh = _num(a, "window_height_m")
    if wh and "window_width" in na: expr += f" - window_width * {wh:g}"; notes.append(f"window openings deducted at {wh:g} m height")
    return {"op": "sum", "kind": "room", "expr": _waste(expr, a, notes), "unit": "m²", "as": "wall area", "notes": notes}, None

def _room_perimeter(p, st, na):
    if "perimeter" not in na: return None, "this drawing has no room outlines"
    return {"op": "sum", "kind": "room", "attr": "perimeter", "notes": ["perimeter = full length of the room outline, door openings included"]}, None

def _opening_schedule(p, st, na):
    a = p.get("args") or {}; k = a.get("kind") or "both"
    kinds = [x for x in (["door", "window"] if k == "both" else [k]) if x in st.kinds()]
    if not kinds: return None, "no doors or windows were found on this drawing"
    names = sorted({str(r.get("name")) for r in st.of("room") if r.get("name") and all(_m(r, f) for f in p.get("filters") or [])}) if p.get("filters") else None
    f = [{"field": "rooms", "op": "regex", "value": "|".join(__import__("re").escape(n) for n in names)}] if names else []
    if p.get("filters") and not names: return None, "no room matched the filters"
    if not any(e.get("rooms") for kd in kinds for e in st.of(kd)) and f: return None, "doors and windows on this drawing are not linked to rooms"
    out = {"op": "count" if a.get("count_only") else "list", "kind": kinds if len(kinds) > 1 else kinds[0], "fields": ["rooms", "width"], "notes": ["openings whose wall gap touches the chosen room(s)"], "_filters": f}
    return out, None

def _room_table(p, st, na):
    return {"op": "list", "kind": "room", "fields": [k for k in ("name", "area", "perimeter", "door_width", "baseboard", "doors", "windows", "building", "rtype") if any(r.get(k) is not None for r in st.of("room"))],
            "notes": ["everything the database holds about the chosen rooms"]}, None

def _look(p, st, na):
    if st.fmt != "raster" or not any(r.geom and len(r.geom) >= 3 for r in st.of("room")): return None, "a crop needs a drawing image with located rooms; this file has none"
    return {"op": "list", "kind": "room", "fields": ["name"], "look": True, "notes": ["the answer is read by a vision model from a crop of the room; it is not computed"]}, None

def _m(e, f):
    from .execute import _match
    try: return _match(e, f)
    except Exception: return False

def _regcheck(p, st, na):
    if not any(r.geom and len(r.geom) >= 3 for r in st.of("room")): return None, "a plan check needs traced rooms; this drawing has none"
    return {"op": "list", "kind": "room", "fields": ["name"], "regcheck": True, "notes": ["each finding names the room, the measured value, the required value and the clause"]}, None

REGISTRY = {"regulation_check": _regcheck, "baseboard": _baseboard, "floor_area": _floor_area, "wall_area": _wall_area, "room_perimeter": _room_perimeter,
            "opening_schedule": _opening_schedule, "room_table": _room_table, "look_at_room": _look}

def compile(plan, store):
    """recipe call -> ordinary plan. The original call is kept under 'recipe'/'args' so the trace can name it."""
    name = plan.get("recipe")
    if name not in REGISTRY: return None, f"unknown recipe {name!r} (available: {sorted(REGISTRY)})"
    if "room" not in store.kinds(): return None, "this drawing has no rooms in its database"
    out, err = REGISTRY[name](plan, store, numeric_attrs(store, "room"))
    if err: return None, err
    out["filters"] = out.pop("_filters", None) if "_filters" in out else list(plan.get("filters") or [])
    out["filters"] = out["filters"] or []
    out["recipe"] = name; out["args"] = plan.get("args") or {}
    out["notes"] = [f"recipe '{name}'"] + out.get("notes", []) + list(plan.get("notes") or [])
    if out.get("expr") and out.get("as"): out["attr"] = out["as"]      # the quantity's name, so the answer reads "add up the baseboard of ..."
    out.setdefault("attr", None); out.setdefault("group_by", None)
    return out, None
