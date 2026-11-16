#!/bin/sh
set -eu

script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
export STOREPILOT_EVIDENCE_DIR=${STOREPILOT_EVIDENCE_DIR:-"$script_dir/../../artifacts/storepilot-compose"}
mkdir -p "$STOREPILOT_EVIDENCE_DIR"
STOREPILOT_EVIDENCE_DIR=$(cd "$STOREPILOT_EVIDENCE_DIR" && pwd)

dc() {
  docker compose -p storepilot-gateway -f "$script_dir/compose.yaml" "$@"
}

app() {
  dc run --rm --no-deps app "$@"
}

root=/evidence/frozen
dc build app
app python -m scripts.storepilot_gateway init "$root"
app python -m scripts.storepilot_gateway run "$root" > "$STOREPILOT_EVIDENCE_DIR/direct.log" 2>&1
app python -m scripts.audit_storepilot_gateway "$root" --direct-only

dc pull envoy
dc run --rm --no-deps envoy --version > "$STOREPILOT_EVIDENCE_DIR/envoy-version.log" 2>&1
dc run --rm --no-deps envoy -c /etc/envoy/storepilot.yaml --mode validate > "$STOREPILOT_EVIDENCE_DIR/envoy-validate.log" 2>&1
dc images --format json > "$STOREPILOT_EVIDENCE_DIR/images.json"
trap 'dc down' EXIT

failed=0
for scenario in normal before-headers client-abort upstream-abort lost-done lost-confirm rollback idle-timeout max-duration ad-worker-abort
do
  dc up -d --no-deps --force-recreate envoy
  if ! dc run --rm --use-aliases --no-deps app python -m scripts.storepilot_gateway run "$root" \
      --label envoy --case "$scenario" --bind 0.0.0.0:5010 --origin http://envoy:8080 \
      > "$STOREPILOT_EVIDENCE_DIR/$scenario.log" 2>&1; then
    failed=1
  fi
  dc logs --no-color --no-log-prefix envoy > "$STOREPILOT_EVIDENCE_DIR/frozen/envoy/$scenario/envoy.log" 2>&1
done
app python -m scripts.audit_storepilot_gateway "$root"
exit "$failed"
