#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
FIXTURE_DIR="$ROOT_DIR/engine/tests/fixtures/e2e"

DESTINATION="${1:-$(mktemp -d /tmp/knowler-e2e-XXXXXX)}"
mkdir -p "$DESTINATION"

cp "$FIXTURE_DIR/transformer_foundations.md" "$DESTINATION/"
cp "$FIXTURE_DIR/chat_model_alignment.md" "$DESTINATION/"
cp "$FIXTURE_DIR/retrieval_augmented_generation.md" "$DESTINATION/"
cp "$FIXTURE_DIR/research-bookmarks.html" "$DESTINATION/"

cat <<EOF
Knowler E2E fixture created at:
  $DESTINATION

Included files:
  - transformer_foundations.md
  - chat_model_alignment.md
  - retrieval_augmented_generation.md
  - research-bookmarks.html

Recommended manual flow:
  1. Run: make dev
  2. Open: http://localhost:5173 for hot reload or http://localhost:7842 for packaged mode
  3. Create a new project from:
     $DESTINATION
  4. After the initial import/build, go to Inbox and import:
     $DESTINATION/research-bookmarks.html
EOF
