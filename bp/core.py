"""Core data model: Entity, Store, Trace, Answer. Everything an answer depends on is recorded in the Trace."""
from dataclasses import dataclass, field
from typing import Any
import hashlib, json

@dataclass
class Entity:
    id: str                      # stable id, e.g. "room:2.04", "door:17", IFC GlobalId
    kind: str                    # canonical kind: room, apartment, door, window, toilet, washbasin, bathtub, shower, wall, slab, ...
    attrs: dict = field(default_factory=dict)   # label, number, rtype, level, apartment, area, length, ...
    geom: Any = None             # list of (x,y) polygon/polyline in drawing coords (or pixel coords for rasters)
    src: dict = field(default_factory=dict)     # provenance: {"file":..., "layer":..., "handle":..., "guid":..., "bbox":...}
    def get(self, k, d=None):
        """attribute, or (for IFC) a property-set value: 'FireRating' or 'props.FireRating', case-insensitive"""
        v = self.attrs.get(k)
        if v is None and isinstance(k, str):
            p = self.attrs.get("props")
            if p:
                k2 = k[6:] if k.startswith("props.") else k
                v = p.get(k2)
                if v is None:
                    kl = k2.lower().replace("_", ""); v = next((x for n, x in p.items() if n.lower().replace("_", "").replace(" ", "") == kl), None)
        return d if v is None else v
    def brief(self):
        a = {k: v for k, v in self.attrs.items() if k in ("label", "number", "rtype", "level", "apartment", "area", "length", "volume", "type_name", "name")}
        return {"id": self.id, "kind": self.kind, **a}

class Store:
    """All entities extracted from one drawing/model plus facts about the extraction (units, scale, warnings)."""
    def __init__(self, path, fmt):
        self.path, self.fmt = path, fmt
        self.entities: list[Entity] = []
        self.meta = {"units": None, "warnings": [], "extractor": None}
    def add(self, e): self.entities.append(e); return e
    def of(self, kind): return [e for e in self.entities if e.kind == kind]
    def kinds(self):
        out = {}
        for e in self.entities: out[e.kind] = out.get(e.kind, 0) + 1
        return out
    def labels(self, kind="room"):
        return sorted({e.get("label") for e in self.of(kind) if e.get("label")})
    def summary(self):
        return {"format": self.fmt, "kinds": self.kinds(), "room_labels": self.labels()[:60],
                "levels": sorted({str(e.get("level")) for e in self.entities if e.get("level") is not None})[:40],
                "apartments": sorted({e.get("name") for e in self.of("apartment") if e.get("name")}),
                "units": self.meta["units"]}

class Trace:
    def __init__(self): self.steps = []
    def add(self, op, text, ids=None, values=None, **kw):
        s = {"op": op, "text": text}
        if ids is not None: s["ids"] = list(ids)[:400]; s["n"] = len(ids)
        if values is not None: s["values"] = values
        s.update(kw); self.steps.append(s); return s

@dataclass
class Answer:
    value: Any = None
    unit: str | None = None
    status: str = "ok"           # ok | refused | error
    reason: str | None = None    # why refused / caveat
    plan: dict | None = None
    plan_source: str | None = None   # rules | llm | llm-cache
    trace: list = field(default_factory=list)
    evidence: list = field(default_factory=list)  # entity ids to highlight
    interpretation: list = field(default_factory=list)
    entity: dict | None = None   # for max/min: which entity won
    def to_dict(self):
        d = self.__dict__.copy()
        d["fingerprint"] = hashlib.sha1(json.dumps([self.value, self.plan, self.trace], sort_keys=True, default=str).encode()).hexdigest()[:12]
        return d
