# Recipes: what the engine can compute, and when to use each one

This file is given to the planner model with every question. It is the model's memory of the scripts that exist.
The model does not match keywords: it reads what each recipe computes and picks the one that answers the question,
in any language or wording. The model never returns a number. It returns which recipe to run and on which rooms;
code in `bp/recipes.py` does the arithmetic on the drawing's data and shows every step.

To add a capability: write one function in `bp/recipes.py`, register it, and add one section here.

How to call a recipe (JSON):

```json
{"recipe": "<name>", "filters": [{"field": "name", "op": "contains", "value": "library"}], "args": {}, "notes": ["why this recipe"]}
```

`filters` choose the rooms (same filter format as a plain plan; use the fields in the DATA DICTIONARY, and the room
names exactly as listed under ROOM LABELS). An empty list means every room on the sheet.

---

## baseboard
Length of baseboard (skirting board, floor trim, plinth) needed along the walls of the chosen rooms.
- Formula per room: `perimeter − total width of the door openings on that room's boundary`.
- Use for any question about the running length of something fixed along the foot of the walls.
- args: `waste_percent` (number, default 0): added on top, e.g. 10 for "with 10% waste".
- Unit: m. Draws the chosen rooms with their length.
- If the drawing has no door widths linked to rooms, the plain perimeter is used and the answer says so.

## floor_area
Floor area of the chosen rooms, wall face to wall face.
- Formula: sum of `area`.
- args: `group_by` (optional: "name", "rtype", "building", "level", "apartment") for a breakdown;
  `exclude` (optional list of filters) for rooms to leave out, e.g. "apartment area without the bathroom".
- Unit: m². Draws the chosen rooms with their area.

## wall_area
Area of the wall surfaces of the chosen rooms, for paint, plaster or tiling.
- Formula per room: `(perimeter − door widths) × height`, minus `window width × window_height` when a window
  height is given.
- args: `height_m` (required: the ceiling height is not on a floor plan; take it from the question. If the
  question gives none, do not guess: return this recipe without it and the engine will ask for it);
  `window_height_m` (optional); `waste_percent` (optional).
- Unit: m².

## room_perimeter
Perimeter of the chosen rooms (the full outline, door openings included).
- Formula: sum of `perimeter`. Unit: m.

## opening_schedule
List of the doors and windows of the chosen rooms with their widths.
- Use for "which doors does the library have", "how wide is the kitchen door", "how many windows in the hall".
- args: `kind` ("door", "window" or "both", default "both"); `count_only` (true to return just the number).

## room_table
Everything the database holds about the chosen rooms: name, area, perimeter, doors, windows, building.
- Use when the question asks to describe or compare rooms, or asks for several of these values at once.

## look_at_room
Send a full-resolution crop of the chosen room(s) to a vision model together with the question.
- Use ONLY when the answer is visible on the drawing but is not in the database: furniture, fixtures that are not a
  listed kind, hatching or floor finish symbols, notes written inside the room, how a room is laid out.
- Never use it for a quantity that another recipe can compute (areas, lengths, counts of listed kinds).
- The answer is labelled as read by a model from the crop, with the crop shown. It is never marked as checked.

## regulation_check
Check the plan against the written rules in `regulations/rules.json` (fire warning system and detector coverage,
classroom area for the number of seats, ventilation per person).
- Use when the question asks whether the plan complies with regulations, rules, codes or standards, asks to check or
  verify the plan, or asks what would fail an inspection.
- No filters and no args: the whole sheet is checked.
- Returns findings, each one passed, failed or not checked, with the room, the measured and the required value and
  the exact clause. Failing rooms are highlighted.

---

If no recipe fits but the data dictionary has what is needed, return a plain plan. A plain plan may use
`"expr"`: a formula over one row's numeric attributes, e.g. `{"op": "sum", "kind": "room", "expr": "area * 0.15",
"unit": "m³", "as": "screed volume"}`. Allowed in a formula: attribute names from the dictionary, numbers,
`+ - * /` and brackets.

If the drawing does not hold what the question needs and looking at a crop would not help, return
`{"op": "refuse", "notes": ["what is missing"]}`.
