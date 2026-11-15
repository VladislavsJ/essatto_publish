"""Generate the question bank for the solved-question memory with a small model (Claude Haiku through its command line).

  python memory/generate_questions.py            -> memory/questions_train.jsonl, memory/questions_test.jsonl

For each pattern in patterns.json Haiku writes questions in five languages. A second, separate request writes the test
questions and is told to avoid the wording of the first set, so the test measures new phrasings, not memorised ones.
Nothing here is an answer: the bank only pairs a question with the pattern (the way to solve it)."""
import json, os, re, subprocess, sys
from concurrent.futures import ThreadPoolExecutor
HERE = os.path.dirname(os.path.abspath(__file__)); P = json.load(open(os.path.join(HERE, "patterns.json")))
LANGS = ["English", "Finnish", "German", "Swedish", "Norwegian"]
ROOMS = "kitchen, library, classroom, corridor, staff office, hall, plant room, meeting room, toilet, reception, store, lobby"
def ask(prompt):
    r = subprocess.run(["claude", "-p", "--model", "haiku", "--output-format", "json", "--tools", "", "--no-session-persistence"], input=prompt, capture_output=True, text=True, timeout=300)
    out = json.loads(r.stdout)["result"]; m = re.search(r"\[.*\]", out, re.S); return json.loads(m.group(0))
def gen(p, n_per_lang, avoid=None):
    extra = ("Write them DIFFERENTLY from these existing ones (other words, other sentence shapes, some very short, some chatty, some with typos):\n" + "\n".join("- " + q for q in avoid[:25])) if avoid else \
            "Vary the wording a lot: formal, casual, terse site-worker style, trade words, a few with small typos."
    room = "Use different room names from this list where a room is needed (translate the room name into the question's language): " + ROOMS + "." if "room" in p["id"] and p["id"] not in ("count_rooms", "largest_room") else "Do not name a specific room."
    prompt = (f"Write questions a construction worker or estimator would ask about a floor plan drawing. Every question must ask for exactly this: {p['what']}.\n"
              f"{room}\n{extra}\nWrite {n_per_lang} questions in each of: {', '.join(LANGS)}.\n"
              'Answer with a JSON array only: [{"lang": "English", "q": "..."}, ...]')
    try: return [{"pattern": p["id"], "lang": x["lang"], "q": x["q"].strip()} for x in ask(prompt) if x.get("q")]
    except Exception as e: print("FAILED", p["id"], repr(e)[:120], file=sys.stderr); return []
with ThreadPoolExecutor(2) as ex: train = list(ex.map(lambda p: gen(p, 8), P))
with ThreadPoolExecutor(2) as ex: test = list(ex.map(lambda a: gen(a[0], 3, [x["q"] for x in a[1]]), zip(P, train)))
for name, rows in (("questions_train.jsonl", train), ("questions_test.jsonl", test)):
    flat = [x for r in rows for x in r]; open(os.path.join(HERE, name), "w").write("".join(json.dumps(x, ensure_ascii=False) + "\n" for x in flat))
    print(name, len(flat), {p["id"]: sum(1 for x in flat if x["pattern"] == p["id"]) for p in P})
