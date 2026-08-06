#!/usr/bin/env bash
set -euo pipefail

batch="${JP_ENRICH_BATCH_SIZE:-5}"
remaining_limit="${JP_ENRICH_MAX_ITEMS:-0}"

if ! [[ "$batch" =~ ^[1-9][0-9]*$ ]]; then
  echo "JP_ENRICH_BATCH_SIZE must be a positive integer" >&2
  exit 2
fi
if ! [[ "$remaining_limit" =~ ^[0-9]+$ ]]; then
  echo "JP_ENRICH_MAX_ITEMS must be a non-negative integer" >&2
  exit 2
fi

git config user.name "policy-bot"
git config user.email "bot@users.noreply.github.com"

for iteration in $(seq 1 200); do
  run_limit="$batch"
  if (( remaining_limit > 0 && remaining_limit < batch )); then
    run_limit="$remaining_limit"
  fi

  before="$(git hash-object docs/data.json)"
  set +e
  output="$(python scraper/enrich_japan.py \
    --batch-size "$run_limit" --max-items "$run_limit" 2>&1)"
  status=$?
  set -e
  printf '%s\n' "$output"
  after="$(git hash-object docs/data.json)"

  if [[ "$before" != "$after" ]]; then
    git add docs/data.json
    git commit -m "enrich Japan policy checkpoint $(date -u +%F)"
    git pull --rebase
    git push
  fi

  if (( status != 0 )); then
    exit "$status"
  fi
  if [[ "$before" == "$after" ]]; then
    break
  fi

  if (( remaining_limit > 0 )); then
    remaining_limit=$((remaining_limit - run_limit))
    if (( remaining_limit <= 0 )); then
      break
    fi
  fi
done
