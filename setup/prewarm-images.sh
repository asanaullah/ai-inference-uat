#!/usr/bin/env bash
# Assisted by Claude Opus
# Pre-pull ("warm") every container image used by the test library onto each GPU
# node in a cluster config, so the real test runs don't stall or time out on
# first-time image pulls (some images are multi-GB).
#
# For every (node x image) pair it launches a throwaway pod that overrides the
# entrypoint with `true` and exits immediately. The pods request no GPUs but
# tolerate the GPU node taint (nvidia.com/gpu.product:NoSchedule) and pin to a
# specific node, so the kubelet on that node must pull and cache the image before
# the container can run. Once every pod has completed (image cached), the pods
# are deleted.
#
# Usage:
#   setup/prewarm-images.sh <cluster.yaml> [namespace]
#
# Env:
#   OC               oc invocation to use (default: "oc"); e.g. OC="oc --as system:admin"
#   PREWARM_TIMEOUT  per-pod readiness wait, seconds (default: 1200)
#
# The namespace defaults to the cluster config's spec.namespace and must already
# exist. The image list is extracted from test_lib/*.yaml (see IMAGES below);
# refresh it if the test library's images change.
set -euo pipefail

if [[ $# -lt 1 || "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then
  grep '^#' "$0" | sed 's/^# \{0,1\}//'
  exit 1
fi

CLUSTER="$1"
OC="${OC:-oc}"
PREWARM_TIMEOUT="${PREWARM_TIMEOUT:-1200}"
LABEL="app=image-prewarm"

if [[ ! -f "$CLUSTER" ]]; then
  echo "error: cluster config not found: $CLUSTER" >&2
  exit 1
fi

# Images used across the test library (test_lib/*.yaml).
IMAGES=(
  "docker.io/networkstatic/iperf3:latest"
  "docker.io/nginx:1.27-alpine"
  "ghcr.io/llm-d/llm-d-cuda:v0.8.0"
  "ghcr.io/llm-d/llm-d-router-disagg-sidecar:v0.9.0"
  "ghcr.io/vllm-project/guidellm:v0.6.1"
  "nvcr.io/nvidia/vllm:26.03-py3"
  "quay.io/inference-perf/inference-perf:v0.6.1"
  "quay.io/jschless/ml-dev-env:pytorch-2.9"
  "registry.redhat.io/ubi9/ubi:9.8"
)

# Pull node names and default namespace out of the cluster config.
mapfile -t NODES < <(python3 -c "
import sys, yaml
d = yaml.safe_load(open('$CLUSTER'))
print('\n'.join(n['name'] for n in d['spec']['nodes']))
")
NS="${2:-$(python3 -c "import yaml; print(yaml.safe_load(open('$CLUSTER'))['spec']['namespace'])")}"

if [[ ${#NODES[@]} -eq 0 ]]; then
  echo "error: no nodes found in $CLUSTER" >&2
  exit 1
fi

echo "Cluster config : $CLUSTER"
echo "Namespace      : $NS"
echo "Nodes          : ${NODES[*]}"
echo "Images         : ${#IMAGES[@]}"
echo "Pods to launch : $(( ${#NODES[@]} * ${#IMAGES[@]} ))"
echo

cleanup() {
  echo
  echo "Cleaning up prewarm pods..."
  $OC delete pod -n "$NS" -l "$LABEL" --ignore-not-found --wait=false >/dev/null 2>&1 || true
}
trap cleanup EXIT

# Launch one pod per (node, image). All pods are submitted up front so their
# image pulls run in parallel across nodes; the wait below blocks until they all
# finish. Names are prefixed with node and image indices (prewarm-n<ni>-i<ii>-...)
# so they are unique per pair even if two images share a basename.
ni=0
for node in "${NODES[@]}"; do
  ii=0
  for image in "${IMAGES[@]}"; do
    # Readable, DNS-1123-safe pod name: prewarm-n<node-idx>-i<image-idx>-<basename>.
    token="${image%%:*}"; token="${token##*/}"
    name="prewarm-n${ni}-i${ii}-${token}"
    name="$(printf '%s' "$name" | tr '[:upper:]' '[:lower:]' | tr -c 'a-z0-9-' '-' | cut -c1-63)"
    $OC apply -n "$NS" -f - >/dev/null <<EOF
apiVersion: v1
kind: Pod
metadata:
  name: ${name}
  namespace: ${NS}
  labels:
    ${LABEL%%=*}: ${LABEL##*=}
spec:
  nodeSelector:
    kubernetes.io/hostname: ${node}
  tolerations:
    - key: nvidia.com/gpu.product
      operator: Exists
      effect: NoSchedule
  restartPolicy: Never
  containers:
    - name: prewarm
      image: ${image}
      command: ["true"]
EOF
    echo "  launched ${name} -> ${node} (${image})"
    ii=$((ii + 1))
  done
  ni=$((ni + 1))
done

# Pull-stage failures: these mean the image is NOT on the node. Anything else
# (Succeeded, Running, or a post-pull runtime error like CreateContainerError --
# which happens on minimal images that lack the `true` binary) means the image
# pulled successfully, which is all we care about for caching.
PULL_FAILED_REASONS="ImagePullBackOff ErrImagePull ErrImageNeverPull InvalidImageName ImageInspectError RegistryUnavailable"

# Returns pods still in the pull stage (scheduling or pulling), one per line.
pending_pods() {
  $OC get pods -n "$NS" -l "$LABEL" \
    -o jsonpath='{range .items[*]}{.metadata.name}{" "}{.status.phase}{" "}{.status.containerStatuses[0].state.waiting.reason}{"\n"}{end}' \
  | while read -r pname phase reason; do
      [[ -z "$pname" ]] && continue
      [[ "$phase" == "Succeeded" || "$phase" == "Running" || "$phase" == "Failed" ]] && continue
      # Past the pull stage (image present) -> not pending.
      case "$reason" in
        CreateContainerError|CreateContainerConfigError|RunContainerError|CrashLoopBackOff|StartError|PostStartHookError)
          continue ;;
      esac
      # Genuine pull failure -> not pending (won't resolve by waiting).
      for r in $PULL_FAILED_REASONS; do
        [[ "$reason" == "$r" ]] && continue 2
      done
      echo "$pname"
    done
}

echo
echo "Waiting for images to cache (timeout ${PREWARM_TIMEOUT}s)..."
deadline=$(( $(date +%s) + PREWARM_TIMEOUT ))
while :; do
  n_pending="$(pending_pods | grep -c . || true)"
  [[ "$n_pending" -eq 0 ]] && break
  if [[ "$(date +%s)" -ge "$deadline" ]]; then
    echo "WARNING: timed out with $n_pending pod(s) still pulling." >&2
    break
  fi
  sleep 10
done

echo
echo "Final status:"
$OC get pods -n "$NS" -l "$LABEL" -o wide

# Report any image that genuinely failed to pull.
NOT_CACHED=0
while read -r pname phase reason; do
  [[ -z "$pname" ]] && continue
  for r in $PULL_FAILED_REASONS; do
    if [[ "$reason" == "$r" ]]; then
      echo "  NOT CACHED: $pname ($reason)" >&2
      NOT_CACHED=$((NOT_CACHED + 1))
    fi
  done
done < <($OC get pods -n "$NS" -l "$LABEL" \
  -o jsonpath='{range .items[*]}{.metadata.name}{" "}{.status.phase}{" "}{.status.containerStatuses[0].state.waiting.reason}{"\n"}{end}')

echo
if [[ "$NOT_CACHED" -ne 0 ]]; then
  echo "WARNING: $NOT_CACHED image(s) failed to cache (see above)." >&2
  exit 1
fi
echo "All images cached on all nodes."
