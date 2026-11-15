#!/bin/sh
# Downloads the large detector (118 MB), which is over GitHub's 100 MB limit for a file in a repository and is
# therefore attached to a release instead. Run from the repository root:   sh models/fetch_weights.sh
set -e
REPO="${REPO:-VladislavsJ/essatto}"; TAG="${TAG:-weights-v1}"
F=floorplan_detector_yolo26x_1536_doors.pt
SHA=dc9490cf63d7d7a9325cf8f29154bb56956b5ee700de4e15f081280164e2ae7c
cd "$(dirname "$0")"
if [ -f "$F" ] && echo "$SHA  $F" | sha256sum -c - >/dev/null 2>&1; then echo "$F is already here and intact"; exit 0; fi
echo "Downloading $F from the $TAG release of $REPO"
curl -L --fail -o "$F.part" "https://github.com/$REPO/releases/download/$TAG/$F"
echo "$SHA  $F.part" | sha256sum -c - || { echo "The download does not match the expected checksum; it was not kept."; rm -f "$F.part"; exit 1; }
mv "$F.part" "$F"; echo "Saved models/$F"
