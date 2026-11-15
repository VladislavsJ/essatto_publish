import json, re, os
PREVIEW = "/mnt/Shared_w_L/Hackathon_Nov13/STOA/junction-dataset-preview"
FULL = "./junction-dataset"
ROOT = os.environ.get("BP_DATA", FULL if os.path.isdir(FULL) else PREVIEW)
def load_questions():
    return [json.loads(l) for l in open(f"{ROOT}/questions.jsonl")]
def source(q): return q["path"].split("/")[1]
