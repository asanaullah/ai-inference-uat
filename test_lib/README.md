# Test Library

This directory contains the test definitions used to validate AI inference platforms on OpenShift. Each test consists of a YAML definition that the UAT framework consumes and a Ginkgo test file that gets compiled into a binary and run inside a pod. A separate test suite YAML file controls which tests run, in what order, and what happens when one fails.

## Tests

1. [platform-check](#1-platform-check) — RBAC and API group validation
2. [component](#2-component) — Node hardware and software validation
3. [guidellm](#3-guidellm) — Single-GPU inference benchmark (guidellm)
4. [inference-perf](#4-inference-perf) — Single-GPU inference benchmark (inference-perf)
5. [llm-d-local](#5-llm-d-local) — Colocated prefill/decode disaggregated inference with NIXL over NVLink
6. [chunked-prefill](#6-chunked-prefill) — All-GPU tensor-parallel inference with chunked prefill
7. [iperf3](#7-iperf3) — Inter-node TCP bandwidth

---

## 1. platform-check

### Purpose

This test checks whether the cluster has the right RBAC permissions and API groups in place before any workloads get deployed. On the RBAC side, it confirms that the test pod's ServiceAccount is properly locked down and should not be able to read secrets, create pods, or access node information. On the API group side, it confirms that the CRDs needed by later tests (KubeFlow, KServe, Ray) are actually installed. Running this first means you find out about missing platform prerequisites before spending time on GPU-heavy tests that would just fail anyway.

### Architecture

```
checker [pass-fail, ephemeral]
    creates SelfSubjectAccessReviews (RBAC checks)
    queries API server discovery endpoint (API group checks)
```

This is a single ephemeral pod with no GPUs, no server, no service, and no sweep phase.

### Infrastructure

| Name | Image | GPUs | Service | Key config |
|------|-------|------|---------|------------|
| checker | `registry.redhat.io/ubi9/ubi:latest` | none | none | `PERMISSION_CHECKS` env (JSON array of check objects) |

The list of checks to run is defined in the test YAML and passed to the pod as a JSON array through the `PERMISSION_CHECKS` environment variable. Each entry in the array has a `type` (either `permission` or `apiGroup`) and an `expected` value (`"yes"` or `"no"`). The user can add, remove, or change the expected value for any check in the YAML to match their cluster's configuration. The test goes through each check, compares the actual result against the expected value, and fails if any of them do not match.

### Pass-fail assertions

The default checks included in the YAML are listed below, but these are fully configurable by the user.

**Permission checks** are expected to be denied, which confirms the pod is properly sandboxed:

1. Cannot `get` pods
2. Cannot `create` pods
3. Cannot `get` secrets
4. Cannot `get` nodes

**API group checks** confirm required CRDs are installed:

5. `kubeflow.org` is present
6. `serving.kserve.io` is present
7. `ray.io` is present
8. `nonexistent.example.io` is absent (negative control)

### Prerequisites

The in-cluster Kubernetes client must work from the pod (`rest.InClusterConfig()`). No special RBAC is needed. The default ServiceAccount is sufficient since the test is specifically checking what permissions it does and does not have.

### Design rationale

The permission checks are there to verify that the test pod is sandboxed. If any of them come back as allowed, the test fails because it means the ServiceAccount has more privileges than it should. This guards against accidentally running the test suite with an overly permissive ServiceAccount. The API group checks verify that the cluster has the CRDs that downstream tests depend on. For example, the llm-d test needs `inference.networking.k8s.io` for InferencePool resources. Finding out about a missing CRD here gives a much clearer error message than a cryptic failure partway through a later test. The `nonexistent.example.io` entry with expected `"no"` is a negative control that confirms the API group check logic is actually working and not just returning `"yes"` for everything.

---

## 2. component

### Purpose

This test checks whether a node's actual hardware and software configuration matches what the cluster specification says it should be. It looks at things like GPU count and model, NVLink connectivity, PCIe link width, CPU and memory specs, kernel version, power management settings, and FIPS compliance. The goal is to surface hardware faults or misconfigurations early, before the more expensive GPU-heavy tests run.

### Architecture

```
test-runner [pass-fail, ephemeral]
    reads cluster spec (embedded at compile time)
    probes nvidia-smi, /proc/*, /sys/*
```

This is a single ephemeral pod. There is no server, no service, and no sweep phase. It just runs the checks and exits.

### Infrastructure

| Name | Image | GPUs | Service | Key config |
|------|-------|------|---------|------------|
| test-runner | `registry.redhat.io/ubi9/ubi:latest` | All node GPUs | none | `NODE_NAME` env; cluster spec embedded via `//go:embed` |

### Pass-fail assertions

The assertions are grouped into three categories:

**Sanity checks** are hard failures that indicate broken or mismatched hardware:

1. GPU count matches cluster spec
2. GPU model matches cluster spec
3. NVLink width matches expected (e.g. NV6, NV12)
4. NVLink topology is all-to-all (every GPU has a direct NVLink path to every other)
5. PCIe link width matches expected (e.g. x16)
6. PCIe generation matches expected (e.g. Gen5)
7. CPU model matches cluster spec
8. CPU count matches cluster spec
9. Memory capacity is within 5% of expected
10. NUMA node count matches expected

**Ideal checks** cover settings that affect performance but are not outright failures:

11. CUDA driver version
12. GPU power limit (watts)
13. GPU persistence mode
14. Kernel version
15. Hugepages (2Mi) count
16. CPU frequency governor (e.g. `performance`)
17. CPU idle driver (e.g. `intel_idle`)
18. CPU idle governor (e.g. `menu`)
19. Only the expected C-states are enabled (e.g. POLL and C1 only)
20. Transparent hugepages setting (e.g. `madvise`)

**Compliance checks** cover regulatory requirements:

21. FIPS mode is enabled (this check is skipped if the cluster spec does not require it)

### Prerequisites

`nvidia-smi` must be available in the container, which is provided by the GPU device plugin. The cluster spec must be present at compile time since it is embedded into the Go binary via `//go:embed`. The pod must request all GPUs on the node so that it can see the full topology.

### Design rationale

The pod requests all of the node's GPUs because `nvidia-smi topo -m` only reports topology for GPUs that are visible to the container. Without requesting all of them, NVLink validation would be incomplete. The cluster spec is embedded at compile time rather than loaded from a file or configmap because the test pod does not have access to the host filesystem and should not depend on external configmaps being present. The memory check allows a 5% tolerance because the kernel reserves memory for page tables, device mappings, and other overhead, so `/proc/meminfo` always reports less than the physical DIMM capacity. NVLink topology validation currently only supports all-to-all because that is the topology used by current SXM-based GPU nodes (DGX, HGX). Other topologies like ring or tree would need different validation logic. The C-state check iterates through sysfs entries under `cpuidle/state*/disable` because deep C-states (C3 and beyond) add wake-up latency that can affect interrupt-driven workloads like RDMA transfers and GPU-to-GPU communication.

---

## 3. guidellm

### Purpose

This test deploys a single-GPU vLLM inference server and benchmarks it using [guidellm](https://github.com/neuralmagic/guidellm). It first validates that the server starts up, loads the model, and can serve requests, then runs a set of parametric benchmarks that measure throughput and latency under different load profiles. This gives a baseline of single-GPU inference performance.

### Architecture

```
vllm-server [persistent]
    |
    +--- pass-fail [ephemeral]
    |
    +--- sweep [ephemeral, one pod per entry]
             short-burst
             sustained-load
             long-context
```

The vLLM server stays up for both the pass-fail and sweep phases. Each sweep entry runs as a separate pod against the same warm server.

### Infrastructure

| Name | Image | GPUs | Service | Key config |
|------|-------|------|---------|------------|
| vllm-server | `nvcr.io/nvidia/vllm:26.03-py3` | 1 | headless ClusterIP :8000 | `--max-model-len=10000 --gpu-memory-utilization=0.6` |
| pass-fail | `ghcr.io/vllm-project/guidellm:v0.6.1` | none | none | `SERVER_URL` env |
| sweep | `ghcr.io/vllm-project/guidellm:v0.6.1` | none | none | `SERVER_URL`, `SWEEP_COMMAND` env |

### Pass-fail assertions

1. HTTP GET `/health` returns 200
2. HTTP GET `/v1/models` returns a non-empty `data` array

### Sweep entries

| ID | Rate | Duration | Prompt tokens | Output tokens |
|----|------|----------|---------------|---------------|
| short-burst | 4 req/s | 30s | 128 | 64 |
| sustained-load | 1 req/s | 120s | 256 | 128 |
| long-context | 1 req/s | 60s | 1024 | 512 |

The sweep step parses `benchmarks.json` from the guidellm output and reports throughput (tokens/s) and request latency (seconds) via Ginkgo `AddReportEntry`.

### Prerequisites

At least one GPU must be available for the server pod to schedule. The models PVC must be mounted at `/models` with the model weights present. The guidellm container image must be accessible from the cluster.

### Design rationale

`gpu-memory-utilization=0.6` leaves headroom to avoid OOM on nodes with smaller GPUs. `max-model-len=10000` caps the context window to reduce GPU memory usage, since this test is about measuring baseline inference performance rather than pushing context length limits. `HOME=/tmp` is set because vLLM writes cache files to `$HOME` and the container user may not have a writable home directory.

---

## 4. inference-perf

### Purpose

This test deploys a single-GPU vLLM inference server and benchmarks it using [inference-perf](https://github.com/kubernetes-sigs/inference-perf) from kubernetes-sigs. It validates that the server starts and loads the model, then runs parametric sweeps that measure throughput and latency under constant and Poisson arrival patterns at different rates with random token length distributions.

### Architecture

```
vllm-server [persistent]
    |
    +--- pass-fail [ephemeral]
    |
    +--- sweep [ephemeral, one pod per entry]
             constant-low
             constant-high
             poisson-burst
```

The vLLM server stays up for both the pass-fail and sweep phases. Each sweep entry runs as a separate pod against the same warm server.

### Infrastructure

| Name | Image | GPUs | Service | Key config |
|------|-------|------|---------|------------|
| vllm-server | `nvcr.io/nvidia/vllm:26.03-py3` | 1 | headless ClusterIP :8000 | `--max-model-len=10000 --gpu-memory-utilization=0.6` |
| pass-fail | `quay.io/inference-perf/inference-perf:latest` | none | none | `SERVER_URL` env |
| sweep | `quay.io/inference-perf/inference-perf:latest` | none | none | `SERVER_URL`, `SWEEP_COMMAND` env |

### Pass-fail assertions

1. HTTP GET `/health` returns 200
2. HTTP GET `/v1/models` returns a non-empty `data` array

### Sweep entries

| ID | Load type | Rate | Duration | Workers | Data |
|----|-----------|------|----------|---------|------|
| constant-low | constant | 1 req/s | 30s | 4 | random (input: 64-256, mean=128; output: 32-128, mean=64) |
| constant-high | constant | 10 req/s | 60s | 4 | same |
| poisson-burst | poisson | 5 req/s | 60s | 4 | same |

The sweep step parses `summary_lifecycle_metrics.json` and asserts that there is at least one successful request and zero failures. It reports throughput (total and output tokens/s), request latency, TTFT, and TPOT via Ginkgo `AddReportEntry`.

### Prerequisites

At least one GPU must be available for the server pod to schedule. The models PVC must be mounted at `/models` with the model weights present. The inference-perf container image must be accessible from the cluster.

### Design rationale

`gpu-memory-utilization=0.6` leaves headroom to avoid OOM on nodes with smaller GPUs. `max-model-len=10000` caps the context window to reduce GPU memory usage. `HOME=/tmp` is set because vLLM writes cache files to `$HOME` and the container user may not have a writable home directory. inference-perf uses dot-separated flag names (`server.type`, `load.stages`, etc.) which the framework converts to `--server.type=vllm` style flags. `server.ignore_eos=true` forces the model to generate exactly the requested number of output tokens regardless of EOS token generation, which ensures consistent benchmark results across runs. `load.stages` is a JSON string within a YAML string, so watch for quoting issues when modifying the sweep entries.

---

## 5. llm-d-local

### Purpose

This test validates KV cache transfer performance over NVLink using NIXL's `cuda_ipc` transport. It deploys a colocated prefill/decode disaggregated inference setup where both vLLM instances, the routing sidecar, and an nginx header injector all run inside a single pod. The GPUs are split evenly between prefill and decode so that each prefill GPU has a 1-to-1 transfer partner on the decode side. The `cuda_ipc` transport requires all containers to share an IPC namespace, which in Kubernetes means they must be in the same pod. Because of this constraint, there is no EPP (Endpoint Picker) or Envoy gateway. Instead, an nginx sidecar injects the `x-prefiller-host-port` header that the routing sidecar needs to orchestrate the P/D protocol.

### Architecture

```
pd-server [persistent, single pod with 4 containers]
    |
    |  main container: decode vLLM :8200 + prefill vLLM :8100
    |                  (two processes in one container, split by CUDA_VISIBLE_DEVICES)
    |
    |  sidecar: routing-sidecar :8000
    |           (orchestrates P/D protocol between prefill and decode)
    |
    |  sidecar: nginx header-injector :8080
    |           (injects x-prefiller-host-port header, serves as entry point)
    |
    +--- pass-fail [ephemeral]
    |
    +--- sweep [ephemeral]
             stress (ramped: 0.5 → 1 → 1.5 → 2 req/s)
```

Request flow: client → nginx :8080 → routing-sidecar :8000 → prefill :8100 computes KV cache → NIXL transfers KV over cuda_ipc/NVLink → decode :8200 generates tokens → response back to client.

Both prefill and decode run as separate processes in the same container, split by `CUDA_VISIBLE_DEVICES`. This is necessary because the Kubernetes NVIDIA device plugin isolates GPUs per container, and `cudaIpcOpenMemHandle` cannot map a remote GPU's memory if that GPU is not in the local CUDA context. With a single container owning all GPUs and using `CUDA_VISIBLE_DEVICES` to partition them, cuda_ipc can establish NVLink peer-to-peer mappings directly.

### Infrastructure

| Name | Image | GPUs | Service | Key config |
|------|-------|------|---------|------------|
| pd-server (main) | `ghcr.io/llm-d/llm-d-cuda:v0.8.0` | All GPUs | headless ClusterIP :8080 | Prefill TP = g/2, Decode TP = g - g/2, UCX/NIXL env |
| routing-sidecar | `ghcr.io/llm-d/llm-d-router-disagg-sidecar:main` | (shared) | :8000 | `--kv-connector=nixlv2` |
| header-injector | `docker.io/nginx:1.27-alpine` | (shared) | :8080 | Injects `x-prefiller-host-port: localhost:8100` |
| pass-fail | `registry.redhat.io/ubi9/ubi:latest` | none | none | `PREFILL_URL`, `DECODE_URL`, `GATEWAY_URL`, `MODEL_NAME`, `PREFILL_TP`, `DECODE_TP` |
| sweep | `quay.io/inference-perf/inference-perf:latest` | none | none | `SERVER_URL` (nginx :8080), `SWEEP_COMMAND` |

### Pass-fail assertions

1. Prefill server is healthy, has model loaded, and serves inference
2. Decode server is healthy, has model loaded, and serves inference
3. Gateway (nginx → sidecar → P/D pipeline) routes disaggregated inference end-to-end
4. GPU split is reported (prefill TP and decode TP)

### Sweep entries

| ID | Load profile | Duration | Data |
|----|-------------|----------|------|
| stress | Ramped stages: 0.5 → 1 → 1.5 → 2 req/s | 4 × 60s | shared_prefix (500 groups × 1 prompt, 16000 system + 250 question tokens, 128 output tokens), worker_max_concurrency=1 |

### Prerequisites

At least 2 GPUs must be available (1 for prefill, rest for decode). The models PVC must be mounted at `/models` with the model weights present. Shared memory (`/dev/shm`) must support 16Gi for NCCL and NIXL IPC.

### Design rationale

Block size is computed from GPU memory using the formula `((mem + 2560) // 5120) * 64`. With cross-layer blocks enabled, NIXL can merge adjacent blocks into a single descriptor and transfer, so contiguous allocation is critical for bandwidth.

The stress test is specifically structured to minimize KV cache fragmentation, which is the main obstacle to achieving high transfer bandwidth. There are two sources of fragmentation. First, concurrent requests cause vLLM's block allocator to interleave block IDs across requests, which defeats NIXL descriptor merging and drops per-transfer bandwidth significantly. The stress test uses `worker_max_concurrency=1` to serialize requests and avoid this. Second, prefix caching causes fragmentation because when vLLM retains KV cache blocks for shared prefixes, new requests reuse those cached prefix blocks but allocate fresh blocks for the unique suffix in scattered free slots between cached entries. The stress test uses `num_groups=500, num_prompts_per_group=1` so that each request has a unique prefix and never gets a cache hit, keeping block allocation contiguous.

`UCX_RNDV_PIPELINE_SHM_ENABLE=no` disables the default behavior of staging GPU-to-GPU transfers through host shared memory, forcing direct cuda_ipc transfers over NVLink. NIXL side-channel ports must differ between prefill (5557) and decode (5558) since both bind to the pod IP.

---

## 6. chunked-prefill

### Purpose

This test deploys a single vLLM instance that uses all available GPUs under one tensor-parallel group with chunked prefill enabled. All GPUs work together in a single TP group where inter-GPU communication happens through NCCL allreduce over NVLink. This validates multi-GPU inference performance without the overhead of KV cache transfer between separate instances.

### Architecture

```
vllm-server [persistent]
    |
    +--- pass-fail [ephemeral]
    |
    +--- sweep [ephemeral]
             stress (ramped: 0.5 → 1 → 1.5 → 2 req/s)
```

The vLLM server stays up for both the pass-fail and sweep phases.

### Infrastructure

| Name | Image | GPUs | Service | Key config |
|------|-------|------|---------|------------|
| vllm-server | `ghcr.io/llm-d/llm-d-cuda:v0.8.0` | All GPUs | headless ClusterIP :8000 | `--tensor-parallel-size=<all GPUs> --enable-chunked-prefill` |
| pass-fail | `registry.redhat.io/ubi9/ubi:latest` | none | none | `SERVER_URL`, `MODEL_NAME` env |
| sweep | `quay.io/inference-perf/inference-perf:latest` | none | none | `SERVER_URL`, `SWEEP_COMMAND` env |

### Pass-fail assertions

1. Server is healthy (HTTP GET `/health` returns 200)
2. Model is loaded (HTTP GET `/v1/models` returns non-empty `data` array)
3. Server serves inference requests (POST `/v1/completions` returns valid response)

### Sweep entries

| ID | Load profile | Duration | Data |
|----|-------------|----------|------|
| stress | Ramped stages: 0.5 → 1 → 1.5 → 2 req/s | 4 × 60s | shared_prefix (500 groups × 1 prompt, 16000 system + 250 question tokens, 128 output tokens), worker_max_concurrency=1 |

### Prerequisites

All GPUs on the scheduling target must be available since the server requests all of them for tensor parallelism. The models PVC must be mounted at `/models` with the model weights present. Shared memory (`/dev/shm`) must support 16Gi for NCCL.

### Design rationale

Chunked prefill allows vLLM to interleave prefill and decode batches, which improves GPU utilization and reduces time-to-first-token under load. The `--enable-chunked-prefill` flag is passed as a bare flag (value `~` in YAML) since it takes no argument.

---

## 7. iperf3

### Purpose

This test measures TCP bandwidth between every pair of nodes in the cluster using iperf3. It validates that the pod network can sustain the throughput needed for inter-node communication such as NCCL allreduce over TCP or KV cache transfers without RDMA.

### Architecture

```
iperf-server [persistent, node 0 of pair]
    |
    +--- iperf-client [pass-fail, ephemeral, node 1 of pair]
```

The test uses `permutation` placement with `setSize: 2`, so for N nodes it produces N*(N-1) test runs covering every ordered pair (A→B and B→A are separate tests). This catches asymmetric bandwidth issues caused by network topology or switch oversubscription.

### Infrastructure

| Name | Image | GPUs | Service | Key config |
|------|-------|------|---------|------------|
| iperf-server | `networkstatic/iperf3:latest` | none | headless ClusterIP :5201 | `iperf3 -s` |
| iperf-client | `networkstatic/iperf3:latest` | none | none | `SERVER_HOST` env |

### Pass-fail assertions

1. TCP connection to server on port 5201 succeeds within 10 seconds
2. `iperf3 -c <host> -t 10 -J` runs successfully and measured bandwidth is greater than zero

Bandwidth in Gbps is reported via Ginkgo `AddReportEntry`.

### Prerequisites

At least 2 nodes must be present in the cluster. The pod network must allow TCP connections on port 5201 between nodes. The iperf3 container image must be accessible from the cluster.

### Design rationale

The test uses `permutation` placement rather than `combination` because A→B and B→A are tested separately. Asymmetric bandwidth (due to network topology, switch oversubscription, or misconfigured QoS) would only show up if both directions are measured independently. The `-J` flag produces JSON output which is parsed directly for the `bits_per_second` value.
