# Blueprint Site Chat: engine + chat app + floor-plan detector, CPU only.
FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends libglib2.0-0 libgl1 libgomp1 && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt python-multipart \
 && pip install --no-cache-dir torch torchvision --index-url https://download.pytorch.org/whl/cpu \
 && pip install --no-cache-dir ultralytics
COPY . .
RUN mkdir -p cache/detections_yolo && tar -xzf cache/detections_yolo.tar.gz -C cache/detections_yolo --strip-components=1 && rm cache/detections_yolo.tar.gz
ENV BP_DET_PYTHON=/usr/local/bin/python BP_DET_WEIGHTS=/app/models/floorplan_detector_yolo26m_1536.pt BP_DET_REPORT=/app/cache/detector_heldout_report.json \
    BP_MAX_UPLOAD_MB=60 PORT=8080
EXPOSE 8080
CMD ["python", "serve.py"]
