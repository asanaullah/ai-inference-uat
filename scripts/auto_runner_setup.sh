#!/usr/bin/env bash
# Assisted by Claude Opus
# Entrypoint for the uat-runner pod (see setup/auto_runner.yaml). Creates a
# timestamped run directory on the workspace PVC, clones the repo into it,
# records provenance (commit + the exact oc binary used), runs the build, and
# launches auto_runner.py to drive the generated steps step-by-step.
set -euo pipefail

: "${REPO_URL:?REPO_URL is required}"
: "${REPO_REF:?REPO_REF is required}"
: "${BUILD_CMD:?BUILD_CMD is required}"
UAT_WORKSPACE="${UAT_WORKSPACE:-/uat_workspace}"
UAT_BIN="${UAT_BIN:-/uat_bin}"
# Build run-id: where results land under <base_path>/<run-id> in the test
# namespaces. Must match the build's --run-id (default "manual-run"); if you
# override this, pass the same --run-id in BUILD_CMD.
UAT_RUN_ID="${UAT_RUN_ID:-manual-run}"

# Put the staged oc on PATH so the generated build/manual/*.sh scripts find it.
export PATH="${UAT_BIN}:${PATH}"

RUN_ID="$(date -u +%Y%m%d_%H%M%S)"
RUN_DIR="${UAT_WORKSPACE}/runs/${RUN_ID}"
REPO_DIR="${RUN_DIR}/repo"
META_DIR="${RUN_DIR}/meta"
LOG_DIR="${RUN_DIR}/logs"
mkdir -p "${META_DIR}" "${LOG_DIR}"

# pip --user and git config need a writable HOME (pod runs as an arbitrary,
# non-root uid); point it at the run dir on the PVC.
export HOME="${RUN_DIR}"

# Mirror all setup output to the run dir as it is produced.
exec > >(tee -a "${RUN_DIR}/runner.log") 2>&1
echo "=== uat-runner ${RUN_ID} ==="
echo "repo=${REPO_URL} ref=${REPO_REF}"

git clone "${REPO_URL}" "${REPO_DIR}"
cd "${REPO_DIR}"
git checkout "${REPO_REF}"
SHA="$(git rev-parse HEAD)"
echo "resolved commit=${SHA}"

# Provenance: copy the exact oc binary used for this run alongside its version,
# so a run's artifacts fully describe how it was executed.
cp "${UAT_BIN}/oc" "${META_DIR}/oc"
oc version --client > "${META_DIR}/oc-version.txt" 2>&1 || true

cat > "${META_DIR}/meta.json" <<EOF
{
  "run_id": "${RUN_ID}",
  "build_run_id": "${UAT_RUN_ID}",
  "repo_url": "${REPO_URL}",
  "repo_ref": "${REPO_REF}",
  "commit": "${SHA}",
  "build_cmd": "${BUILD_CMD}",
  "oc_binary": "meta/oc",
  "started": "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
}
EOF

echo "=== installing dependencies ==="
python3 -m pip install --user --quiet -r requirements.txt
python3 -m pip install --user --quiet kubernetes

echo "=== build ==="
# Produces build/steps.json + build/manual/*.sh under ${REPO_DIR}.
eval "${BUILD_CMD}"

echo "=== run ==="
# Runs the generated .sh steps step-by-step; captures pod logs/status via the
# kubernetes client. Writes shell + pod logs, timesheet, and status under
# ${LOG_DIR}.
python3 scripts/auto_runner.py build --logs "${LOG_DIR}" --run-id "${UAT_RUN_ID}"

echo "=== done: ${RUN_DIR} ==="
