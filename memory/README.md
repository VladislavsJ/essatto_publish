# Solved-question memory

A small local model recognises what kind of question was asked and hands over the way that kind was solved before.

```
question -> embedding (multilingual-e5-small, CPU, ~30 ms) -> softmax classifier (15 patterns) -> pattern + 3 nearest stored questions
```

- **Pattern** (`patterns.json`): what the question asks, the steps to solve it, the tools used, and the call to run.
- **Bank** (`questions_train.jsonl`): 600 questions in English, Finnish, German, Swedish and Norwegian, each tagged with
  its pattern. Written by Claude Haiku (`generate_questions.py`). It holds no answers.
- **Classifier** (`classifier.npz`): one softmax layer on the embedding, trained in seconds by `python -m bp.memory train`.

How an unknown question is handled (after the rule parser, before the planner model):

1. **Replay.** The classifier is at least 0.70 sure, and every slot of the pattern can be filled from the question and
   the drawing (the room exists on this drawing, the height or percentage is in the text). The pattern is run directly.
   No planner model is called.
2. **Worked examples.** Otherwise the three nearest stored questions and their pattern are put into the planner's
   prompt, so the planner starts from a solution that worked for a similar question.
3. **Nothing stored.** The classifier says "other": the planner works from the recipes and the data dictionary alone.

Every answer's trace shows which of the three happened, the probability and the similar questions.

## Measured (`classifier_report.json`)

Test set: 225 questions written by Haiku in a separate request that was told to avoid the wording of the training set.

| | |
|---|---|
| Pattern recognised correctly | see `test_accuracy` |
| Same test with plain nearest-neighbour similarity, no training | see `nearest_neighbour_baseline` |
| Share of test questions above the replay threshold | see `confident_share` |
| Correct among those | see `accuracy_when_confident` |

The replay threshold (0.70) was chosen by looking at this same test set, so the last figure is optimistic.
Training and test questions come from the same model, so real users' wording may be harder.

## Setup

`memory/fetch_model.sh` downloads the embedding model into `cache/embed/` (not in the repository).
Set `BP_NO_MEMORY=1` to switch the memory off, `BP_MEMORY_REPLAY_MIN` to change the threshold.
