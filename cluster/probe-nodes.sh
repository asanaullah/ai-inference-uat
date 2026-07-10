#!/bin/bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
TARGET_CLUSTER="${TARGET_CLUSTER:-ocp-test}"
CLUSTER_FILE="$SCRIPT_DIR/${TARGET_CLUSTER}.yaml"
AS="--as=system:admin"

if [[ ! -f "$CLUSTER_FILE" ]]; then
  echo "ERROR: Cluster file not found: $CLUSTER_FILE"
  exit 1
fi

NODES=$(python3 -c "
import yaml, sys
with open(sys.argv[1]) as f:
    spec = yaml.safe_load(f)
for node in spec['spec']['nodes']:
    print(node['name'])
" "$CLUSTER_FILE")

if [[ -z "$NODES" ]]; then
  echo "ERROR: No nodes found in $CLUSTER_FILE"
  exit 1
fi
echo "Target cluster: $TARGET_CLUSTER"
echo "Nodes to probe: $NODES"

PROBE_SCRIPT='
nsmi() {
  LD_PRELOAD="/run/nvidia/driver/usr/lib64/libnvidia-ml.so.1 /run/nvidia/driver/usr/lib64/libcuda.so.1" \
    /run/nvidia/driver/usr/bin/nvidia-smi "$@"
}

echo "--- 1. GPU COUNT AND MODEL ---"
nsmi --query-gpu=count,name --format=csv,noheader | head -1

echo ""
echo "--- 2. GPU MEMORY ---"
nsmi --query-gpu=memory.total --format=csv,noheader

echo ""
echo "--- 3. CUDA DRIVER VERSION ---"
nsmi --query-gpu=driver_version --format=csv,noheader | head -1
echo "CUDA (max supported):"
nsmi | sed "s/\x1b\[[0-9;]*m//g" | grep -oP "CUDA Version: \K[0-9.]+"

echo ""
echo "--- 4. PCIe LINK ---"
nsmi --query-gpu=index,pcie.link.width.current,pcie.link.gen.current,pcie.link.width.max,pcie.link.gen.max --format=csv,noheader

echo ""
echo "--- 5. NVLink / GPU TOPOLOGY ---"
nsmi topo -m

echo ""
echo "--- 6. GPU CLOCKS AND POWER ---"
nsmi --query-gpu=index,clocks.current.graphics,clocks.max.graphics,clocks.current.memory,clocks.max.memory,power.draw,power.limit,power.max_limit,persistence_mode --format=csv,noheader

echo ""
echo "--- 7. CPU COUNT ---"
nproc
echo "Model:"
grep -m1 "model name" /proc/cpuinfo | cut -d: -f2 | xargs

echo ""
echo "--- 8. SYSTEM MEMORY ---"
grep MemTotal /proc/meminfo

echo ""
echo "--- 9. HUGEPAGES ---"
grep -i huge /proc/meminfo

echo ""
echo "--- 10. /dev/shm SIZE ---"
if findmnt -n -o SIZE /dev/shm 2>/dev/null; then
  :
else
  echo "[UNAVAILABLE from chroot]"
fi

echo ""
echo "--- 11. NUMA TOPOLOGY ---"
echo "Node count:"
ls -d /sys/devices/system/node/node* | wc -l
echo "Per-node memory (kB):"
for n in /sys/devices/system/node/node*/meminfo; do
  node_id=$(echo "$n" | grep -o "node[0-9][0-9]*")
  total=$(grep MemTotal "$n" | awk "{print \$4}")
  echo "  $node_id: $total kB"
done

echo ""
echo "--- 12. KERNEL VERSION ---"
uname -r

echo ""
echo "--- 13. CPU SLEEP STATES ---"
echo "C-states:"
if [ -d /sys/devices/system/cpu/cpu0/cpuidle ]; then
  for s in /sys/devices/system/cpu/cpu0/cpuidle/state*/; do
    name=$(cat "${s}name")
    disabled=$(cat "${s}disable")
    echo "  $name (disabled=$disabled)"
  done
else
  echo "  cpuidle not available"
fi
echo "Idle driver:"
cat /sys/devices/system/cpu/cpuidle/current_driver 2>/dev/null || echo "[UNAVAILABLE]"
echo "Idle governor:"
cat /sys/devices/system/cpu/cpuidle/current_governor_ro 2>/dev/null \
  || cat /sys/devices/system/cpu/cpuidle/current_governor 2>/dev/null \
  || echo "[UNAVAILABLE]"
'

for node in $NODES; do
  echo ""
  echo "========================================================"
  echo "  PROBING NODE: $node"
  echo "========================================================"
  echo ""

  oc debug node/"$node" $AS -- chroot /host bash -c "$PROBE_SCRIPT" 2>/dev/null \
    || echo "ERROR: oc debug failed for $node"
done
