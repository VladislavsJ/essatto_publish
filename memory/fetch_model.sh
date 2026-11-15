#!/bin/bash
# Downloads the small multilingual embedding model used by the solved-question memory (about 135 MB, CPU only).
# It is not stored in the repository. Without it the memory is switched off and every unknown question goes to the planner.
D="$(cd "$(dirname "$0")/.." && pwd)/cache/embed"; mkdir -p "$D"
for f in onnx/model_quantized.onnx tokenizer.json; do
  o="$D/$(basename $f)"; [ -s "$o" ] || curl -sL -o "$o" "https://huggingface.co/Xenova/multilingual-e5-small/resolve/main/$f"
done
ls -la "$D"
