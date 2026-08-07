#!/usr/bin/env python3
# Assisted by Claude Opus 4.6
# Note: Pod exec uses subprocess + oc exec instead of the kubernetes Python
# client's stream() API because the latter sends WebSocket upgrade requests
# that OpenShift RBAC rejects with 403, while oc exec uses SPDY and works.
"""Scan cluster nodes and populate sanity entries in a cluster YAML.

Reads a cluster config, validates it against ClusterTest model, then for each
node launches a debug pod, scans hardware, and merges detected values into the
existing sanity block. Existing values that differ from detected values produce
a warning but are NOT overwritten.

Usage:
    python3 sanity_scan.py --cluster <cluster.yaml> [options]

Options:
    --cluster <path>    Path to cluster YAML (required)
    --node <name,...>   Comma-separated node names to scan (default: all)
    --image <img>       Container image (default: nvcr.io/nvidia/vllm:26.03-py3)
    --output <path>     Output cluster YAML (default: overwrites input)

Examples:
    python3 sanity_scan.py --cluster cluster/ocp-test.yaml
    python3 sanity_scan.py --cluster cluster/ocp-test.yaml --output cluster/ocp-test-updated.yaml

Requires: pip install kubernetes pydantic pyyaml
Uses the current kubeconfig context (same as oc/kubectl).
"""

import json
import os
import subprocess
import sys
import textwrap
import time

import yaml
from kubernetes import client, config

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from src.models import ClusterTest

DEFAULT_IMAGE = "nvcr.io/nvidia/vllm:26.03-py3"
STANDARD_RESOURCES = {
    "cpu",
    "memory",
    "ephemeral-storage",
    "hugepages-2Mi",
    "hugepages-1Gi",
    "pods",
}

# ---------------------------------------------------------------------------
# In-pod scan script — runs inside the debug pod via oc exec
# ---------------------------------------------------------------------------

IN_POD_SCRIPT = textwrap.dedent(r"""
import glob
import json
import math
import os
import re
import subprocess
import sys


def run(cmd):
    try:
        result = subprocess.run(
            cmd, shell=True, capture_output=True, text=True, timeout=30, check=False
        )
        return result.stdout.strip()
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return ""


def read_file(path):
    try:
        with open(path) as f:
            return f.read().strip()
    except (FileNotFoundError, PermissionError):
        return ""


def scan_gpu():
    query = run(
        "nvidia-smi --query-gpu="
        "name,"
        "pcie.link.width.current,"
        "pcie.link.gen.current,"
        "ecc.mode.current,"
        "compute_mode,"
        "mig.mode.current,"
        "memory.total,"
        "gpu_bus_id"
        " --format=csv,noheader,nounits"
    )
    if not query:
        return {}

    rows = [line.split(", ") for line in query.splitlines() if line.strip()]
    if not rows:
        return {}

    gpu_count = len(rows)
    gpu_name = rows[0][0].strip()
    pcie_width = int(rows[0][1].strip())
    pcie_gen = int(rows[0][2].strip())
    ecc_mode = rows[0][3].strip()
    compute_mode = rows[0][4].strip()
    mig_mode = rows[0][5].strip()
    memory_total_mib = int(rows[0][6].strip())
    bus_ids = [r[7].strip() for r in rows]

    nvlink_width, nvlink_topo = scan_nvlink(gpu_count)

    gpu_numa_nodes = []
    for bus_id in bus_ids:
        short = bus_id.lower().replace("0000:", "")
        numa = read_file(f"/sys/bus/pci/devices/{bus_id}/numa_node")
        if not numa:
            numa = read_file(f"/sys/bus/pci/devices/{short}/numa_node")
        if numa:
            gpu_numa_nodes.append(int(numa))

    result = {
        "nvidia.com/gpu": gpu_count,
        "resourceNames": {
            "nvidia.com/gpu": gpu_name.replace(" ", "-"),
        },
        "pcieWidth": pcie_width,
        "pcieGen": pcie_gen,
        "gpuEccMode": ecc_mode,
        "gpuComputeMode": compute_mode,
        "gpuMigMode": mig_mode,
        "gpuMemoryTotal": memory_total_mib,
    }

    if nvlink_width:
        result["nvlink"] = nvlink_width
    if nvlink_topo:
        result["nvlinkTopology"] = nvlink_topo
    if gpu_numa_nodes:
        result["gpuNumaNode"] = gpu_numa_nodes

    return result


def strip_ansi(s):
    return re.sub(r"\x1b\[[0-9;]*m", "", s)


def scan_nvlink(gpu_count):
    if gpu_count < 2:
        return "", ""

    topo = run("nvidia-smi topo -m")
    if not topo:
        return "", ""

    lines = topo.splitlines()
    header_idx = None
    for i, line in enumerate(lines):
        clean = strip_ansi(line).strip()
        if clean.startswith("GPU") and not clean.startswith("GPU NUMA"):
            header_idx = i
            break

    if header_idx is None:
        return "", ""

    nv_re = re.compile(r"NV(\d+)")
    max_nv = 0
    all_pairs_nvlink = True

    for i in range(header_idx + 1, header_idx + 1 + gpu_count):
        if i >= len(lines):
            break
        cols = strip_ansi(lines[i]).split()
        gpu_idx = 0
        for col in cols[1:]:
            if gpu_idx >= gpu_count:
                break
            if gpu_idx == i - header_idx - 1:
                gpu_idx += 1
                continue
            m = nv_re.match(col)
            if m:
                max_nv = max(max_nv, int(m.group(1)))
            else:
                all_pairs_nvlink = False
            gpu_idx += 1

    nvlink_width = f"NV{max_nv}" if max_nv > 0 else ""
    nvlink_topo = "all-to-all" if all_pairs_nvlink and max_nv > 0 else "partial"

    return nvlink_width, nvlink_topo


def scan_cpu():
    cpuinfo = read_file("/proc/cpuinfo")
    if not cpuinfo:
        return {}

    processors = re.findall(r"^processor\s*:", cpuinfo, re.MULTILINE)
    cpu_count = len(processors)

    model_match = re.search(r"^model name\s*:\s*(.+)$", cpuinfo, re.MULTILINE)
    cpu_model = model_match.group(1).strip() if model_match else ""

    hypervisor = bool(re.search(r"\bhypervisor\b", cpuinfo))
    smt_active = read_file("/sys/devices/system/cpu/smt/active")

    return {
        "cpu": cpu_count,
        "resourceNames": {"cpu": cpu_model},
        "smtActive": smt_active == "1",
        "hypervisorPresent": hypervisor,
    }


def scan_memory():
    meminfo = read_file("/proc/meminfo")
    if not meminfo:
        return {}
    match = re.search(r"^MemTotal:\s+(\d+)\s+kB", meminfo, re.MULTILINE)
    if not match:
        return {}
    total_kb = int(match.group(1))
    gi = total_kb / (1024 * 1024)
    return {"memory": f"{math.floor(gi)}Gi"}


def scan_numa():
    numa_dirs = sorted(glob.glob("/sys/devices/system/node/node[0-9]*"))
    if not numa_dirs:
        return {}

    result = {"numaNodes": len(numa_dirs)}

    numa_memory = {}
    for d in numa_dirs:
        name = os.path.basename(d)
        meminfo = read_file(os.path.join(d, "meminfo"))
        m = re.search(r"MemTotal:\s+(\d+)\s+kB", meminfo)
        if m:
            numa_memory[name] = int(m.group(1))
    if numa_memory:
        values = list(numa_memory.values())
        mean = sum(values) / len(values)
        max_dev = max(abs(v - mean) / mean for v in values) if mean > 0 else 0
        result["numaMemoryBalanced"] = max_dev < 0.1
        result["numaMemoryKb"] = numa_memory

    numa_cpus = {}
    for d in numa_dirs:
        name = os.path.basename(d)
        cpulist = read_file(os.path.join(d, "cpulist"))
        if cpulist:
            numa_cpus[name] = cpulist
    if numa_cpus:
        result["numaCpuList"] = numa_cpus

    return result


def scan_infiniband():
    ib_dir = "/sys/class/infiniband"
    if not os.path.isdir(ib_dir):
        return {"ibDevicePresent": False}
    devices = os.listdir(ib_dir)
    if not devices:
        return {"ibDevicePresent": False}

    result = {"ibDevicePresent": True, "ibDevices": {}}
    for dev in sorted(devices):
        ports_dir = os.path.join(ib_dir, dev, "ports")
        if not os.path.isdir(ports_dir):
            continue
        ports = {}
        for port in sorted(os.listdir(ports_dir)):
            state = read_file(os.path.join(ports_dir, port, "state"))
            link_layer = read_file(os.path.join(ports_dir, port, "link_layer"))
            ports[port] = {
                "state": state.split(":")[-1].strip() if ":" in state else state,
                "linkLayer": link_layer,
            }
        result["ibDevices"][dev] = {"ports": ports}
    return result


def scan_cpuset():
    cpuset = read_file("/sys/fs/cgroup/cpuset.cpus.effective")
    if not cpuset:
        cpuset = read_file("/sys/fs/cgroup/cpuset/cpuset.cpus")
    return {"effectiveCpuset": cpuset} if cpuset else {}


sanity = {}
gpu = scan_gpu()
cpu = scan_cpu()
mem = scan_memory()
numa = scan_numa()
ib = scan_infiniband()
cpuset = scan_cpuset()

if "resourceNames" in gpu and "resourceNames" in cpu:
    gpu["resourceNames"].update(cpu.pop("resourceNames"))

sanity.update(gpu)
sanity.update(cpu)
sanity.update(mem)
sanity.update(numa)
sanity.update(ib)
sanity.update(cpuset)

print(json.dumps(sanity))
""").strip()

# ---------------------------------------------------------------------------
# Orchestrator — runs locally, drives the pod lifecycle
# ---------------------------------------------------------------------------


def get_extended_resources(node_name):
    v1 = client.CoreV1Api()
    node = v1.read_node(node_name)
    allocatable = node.status.allocatable or {}
    return {k: v for k, v in allocatable.items() if k not in STANDARD_RESOURCES}


def create_debug_pod(node_name, namespace, image, extended):
    pod_name = f"sanity-scan-{node_name}"
    resources = client.V1ResourceRequirements(
        requests={k: v for k, v in extended.items()},
        limits={k: v for k, v in extended.items()},
    )
    container = client.V1Container(
        name="scan",
        image=image,
        command=["sleep", "infinity"],
        env=[client.V1EnvVar(name="HOME", value="/tmp")],
        resources=resources,
    )
    pod = client.V1Pod(
        metadata=client.V1ObjectMeta(name=pod_name, namespace=namespace),
        spec=client.V1PodSpec(
            node_selector={"kubernetes.io/hostname": node_name},
            containers=[container],
            restart_policy="Never",
        ),
    )

    v1 = client.CoreV1Api()
    try:
        v1.delete_namespaced_pod(pod_name, namespace)
        log(f"deleted stale pod {pod_name}")
        wait_for_deletion(v1, pod_name, namespace)
    except client.ApiException as e:
        if e.status != 404:
            raise

    v1.create_namespaced_pod(namespace, pod)
    log(f"created pod {pod_name}")
    return pod_name


def wait_for_running(v1, pod_name, namespace, timeout=300):
    deadline = time.time() + timeout
    while time.time() < deadline:
        pod = v1.read_namespaced_pod(pod_name, namespace)
        if pod.status.phase == "Failed":
            raise RuntimeError(f"pod {pod_name} failed")
        if pod.status.phase == "Running":
            return True
        time.sleep(5)
    raise TimeoutError(f"pod {pod_name} not running after {timeout}s")


def wait_for_deletion(v1, pod_name, namespace, timeout=60):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            v1.read_namespaced_pod(pod_name, namespace)
            time.sleep(2)
        except client.ApiException as e:
            if e.status == 404:
                return
            raise
    raise TimeoutError(f"pod {pod_name} not deleted after {timeout}s")


def exec_in_pod(pod_name, namespace, command):
    result = subprocess.run(
        ["oc", "exec", pod_name, "-n", namespace, "--"] + command,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"oc exec failed (rc={result.returncode}): {result.stderr.strip()}"
        )
    return result.stdout


def delete_pod(pod_name, namespace):
    v1 = client.CoreV1Api()
    try:
        v1.delete_namespaced_pod(pod_name, namespace)
        log(f"deleted pod {pod_name}")
    except client.ApiException:
        pass


def scan_node(node_name, namespace, image):
    log(f"querying allocatable resources for {node_name}")
    extended = get_extended_resources(node_name)
    if extended:
        log(f"extended resources: {extended}")
    else:
        log("no extended resources found")

    pod_name = None
    try:
        pod_name = create_debug_pod(node_name, namespace, image, extended)
        v1 = client.CoreV1Api()
        log("waiting for pod to start...")
        wait_for_running(v1, pod_name, namespace)
        log("running hardware scan via oc exec...")
        raw = exec_in_pod(pod_name, namespace, ["python3", "-c", IN_POD_SCRIPT])
        return json.loads(raw)
    finally:
        if pod_name:
            delete_pod(pod_name, namespace)


def compare_and_merge(existing, detected, path=""):
    """Merge detected sanity values into existing. Warn on mismatches."""
    warnings = []
    merged = dict(existing)

    for key, det_val in detected.items():
        full_key = f"{path}.{key}" if path else key

        if key not in existing:
            merged[key] = det_val
            log(f"  added {full_key}: {det_val}")
            continue

        ext_val = existing[key]

        if isinstance(det_val, dict) and isinstance(ext_val, dict):
            sub_merged, sub_warnings = compare_and_merge(ext_val, det_val, full_key)
            merged[key] = sub_merged
            warnings.extend(sub_warnings)
            continue

        if ext_val != det_val:
            msg = f"MISMATCH {full_key}: existing={ext_val}, detected={det_val}"
            warnings.append(msg)
            warn(msg)

    return merged, warnings


def log(msg):
    print(f"[sanity-scan] {msg}", file=sys.stderr)


def warn(msg):
    print(f"[sanity-scan] WARNING: {msg}", file=sys.stderr)


def parse_args():
    args = sys.argv[1:]
    if not args or args[0] in ("-h", "--help"):
        print(__doc__.strip())
        sys.exit(0)

    cluster_path = None
    image = DEFAULT_IMAGE
    output = None
    nodes = None

    i = 0
    while i < len(args):
        if args[i] == "--cluster" and i + 1 < len(args):
            cluster_path = args[i + 1]
            i += 2
        elif args[i] == "--image" and i + 1 < len(args):
            image = args[i + 1]
            i += 2
        elif args[i] == "--output" and i + 1 < len(args):
            output = args[i + 1]
            i += 2
        elif args[i] == "--node" and i + 1 < len(args):
            nodes = args[i + 1].split(",")
            i += 2
        else:
            i += 1

    if not cluster_path:
        print("error: --cluster <path> is required", file=sys.stderr)
        sys.exit(1)

    if output is None:
        output = cluster_path

    return cluster_path, image, output, nodes


def main():
    cluster_path, image, output_path, node_filter = parse_args()

    with open(cluster_path) as f:
        raw_data = yaml.safe_load(f)

    cluster = ClusterTest(**raw_data)
    namespace = cluster.spec.namespace
    log(
        f"validated cluster config: {len(cluster.spec.nodes)} node(s), namespace={namespace}"
    )

    all_warnings = []

    for node in cluster.spec.nodes:
        node_name = node.name

        if node_filter and node_name not in node_filter:
            log(f"--- skipping {node_name} (not in --node filter) ---")
            continue

        log(f"--- scanning {node_name} ---")

        existing_sanity = node.component_validation.sanity.model_dump(by_alias=True)

        config.load_kube_config()
        detected = scan_node(node_name, namespace, image)

        merged, warnings = compare_and_merge(existing_sanity, detected)
        all_warnings.extend([(node_name, w) for w in warnings])

        node_idx = next(
            i for i, n in enumerate(raw_data["spec"]["nodes"]) if n["name"] == node_name
        )
        raw_data["spec"]["nodes"][node_idx]["componentValidation"]["sanity"] = merged

    with open(output_path, "w") as f:
        yaml.dump(raw_data, f, default_flow_style=False, sort_keys=False)

    log(f"wrote {output_path}")

    if all_warnings:
        log(f"\n{len(all_warnings)} mismatch(es) found:")
        for node_name, w in all_warnings:
            log(f"  [{node_name}] {w}")
    else:
        log("no mismatches — all existing values match detected hardware")


if __name__ == "__main__":
    main()
