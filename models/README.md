# Detector weights

All three were fine-tuned on the public CubiCasa5k dataset (CC BY-NC 4.0), so they are for non-commercial use.

| File | Model | Size | Where it is |
|---|---|---|---|
| `floorplan_detector_yolo26m_1536.pt` | YOLO26m at 1536 px, 12 classes. The first version; works alone. | 44 MB | in this folder |
| `floorplan_fixtures_yolo26m_2048.pt` | YOLO26m at 2048 px: toilet, sink, shower, bathtub only | 44 MB | in this folder |
| `floorplan_detector_yolo26x_1536_doors.pt` | YOLO26x at 1536 px, 12 classes, tuned for doors | 118 MB | attached to the `weights-v1` release; `sh models/fetch_weights.sh` downloads it and checks its checksum |
| `detector_report_x1536_fix2048.json` | Count thresholds per class and held-out scores for the YOLO26x + fixture pair | small | in this folder |

GitHub does not accept a file over 100 MB in a repository, and compressing weights barely shrinks them (the large
file is still 110 MB gzipped), which is why the large one is a release download.

## Two ways to run

Smallest (what the Dockerfile uses, no download):

```bash
export BP_DET_WEIGHTS=models/floorplan_detector_yolo26m_1536.pt
export BP_DET_REPORT=cache/detector_heldout_report.json
```

Best measured (what the demo server ran):

```bash
sh models/fetch_weights.sh
export BP_DET_WEIGHTS=models/floorplan_detector_yolo26x_1536_doors.pt
export BP_DET_FIX_WEIGHTS=models/floorplan_fixtures_yolo26m_2048.pt
export BP_DET_REPORT=models/detector_report_x1536_fix2048.json
```

`BP_DET_PYTHON` must point at a Python that has `torch`, `ultralytics` and `ensemble-boxes`.
