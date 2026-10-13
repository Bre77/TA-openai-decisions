#!/usr/bin/env bash
# Build and package the add-on: ./build.sh <version>
# Run from a venv whose python is 3.9 so vendored libs resolve for Splunk's oldest runtime.
set -euo pipefail
version="${1:?usage: ./build.sh <version>}"
app=TA-openai-decisions
ucc-gen build --source package --config globalConfig.json --ta-version "$version" -o output -v --overwrite
find "output/$app" -name ".*" -type f -delete
find "output/$app" -name "__pycache__" -type d -prune -exec rm -rf {} +
if find "output/$app/lib" -type f \( -name '*.so' -o -name '*.pyd' -o -name '*.dll' -o -name '*.dylib' \) -print -quit | grep -q .; then
  echo "unexpected native dependency in output/$app/lib" >&2
  exit 1
fi
mkdir -p dist
ucc-gen package --path "output/$app" -o dist
