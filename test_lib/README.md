# Test Library

This directory contains the test definitions used to validate AI inference platforms on OpenShift. Each test consists of a YAML definition that the UAT framework consumes and a Ginkgo test file that gets compiled into a binary and run inside a pod. A separate test suite YAML file controls which tests run, in what order, and what happens when one fails.

## Categories

### Preflight
Platform prerequisites. Checks that must pass before GPU workloads run.

| Test | Description |
|------|-------------|
| [platform-check](#1-platform-check) | RBAC permissions, API groups, and operator CRD registration |
| [platform-check-admin](#2-platform-check-admin) | Operator health, CR status, DSC components, GPU node labels, required pods |
| [component](#3-component) | Node hardware and software validation |
| [iperf3](#4-iperf3) | Inter-node TCP bandwidth |

### Operator
Model serving through cluster operators and CRDs.

| Test | Description |
|------|-------------|
| [kserve](#6-kserve) | KServe InferenceService deployment and inference validation |

### Single-GPU Performance
Inference benchmarks on a single GPU.

| Test | Description |
|------|-------------|
| [guidellm](#7-guidellm) | vLLM + guidellm benchmark sweeps |
| [inference-perf](#8-inference-perf) | vLLM + inference-perf benchmark sweeps |

### Multi-GPU Performance
Workloads that stress GPU-to-GPU communication fabrics.

| Test | Description |
|------|-------------|
| [chunked-prefill](#9-chunked-prefill) | NCCL over NVLink, tensor-parallel inference |
| [llm-d-local](#10-llm-d-local) | NIXL over NVLink, disaggregated prefill/decode |

### Interactive Environments

| Test | Description |
|------|-------------|
| [dev-env](#5-dev-env) | Jupyter notebook server with CUDA validation |

### Multi-Tenancy
Namespace isolation and cross-tenant performance impact. Isolation tests verify that network boundaries between namespaces are enforced. Contention tests deploy competing GPU workloads in a peer namespace and measure the performance impact on the project namespace under different load intensities.

| Test | Description |
|------|-------------|
| [ping](#11-ping) | Cross-namespace service connectivity |
| [peer-load-high](#12-peer-load-high) | Inference performance under high cross-namespace GPU load |
| [peer-load-low](#13-peer-load-low) | Inference performance under low cross-namespace GPU load |

## Tests


---

## 1. platform-check

### Purpose

This test checks whether the cluster has the right RBAC permissions, API groups, and operator CRDs in place before any workloads get deployed. On the RBAC side, it confirms that the test pod's ServiceAccount is properly locked down and should not be able to read secrets, create pods, or access node information. On the API group side, it confirms that the CRDs needed by later tests (KubeFlow, KServe, Ray) are actually installed. On the CRD side, it verifies that the operators required by the platform (RHOAI, GPU operator, NFD, Serverless, Service Mesh) have registered their custom resource definitions. Running this first means you find out about missing platform prerequisites before spending time on GPU-heavy tests that would just fail anyway.

### Architecture

```
checker [pass-fail, ephemeral]
    creates SelfSubjectAccessReviews (RBAC checks)
    queries API server discovery endpoint (API group and CRD checks)
```

This is a single ephemeral pod with no GPUs, no server, no service, and no sweep phase.

### Infrastructure

| Name | Image | GPUs | Service | Key config |
|------|-------|------|---------|------------|
| checker | `registry.redhat.io/ubi9/ubi:latest` | none | none | `PERMISSION_CHECKS` env (JSON array of check objects) |

The list of checks to run is defined in the test YAML and passed to the pod as a JSON array through the `PERMISSION_CHECKS` environment variable. Each entry in the array has a `type` (`permission`, `apiGroup`, or `crd`) and an `expected` value (`"yes"` or `"no"`). The user can add, remove, or change the expected value for any check in the YAML to match their cluster's configuration. The test goes through each check, compares the actual result against the expected value, and fails if any of them do not match.

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

**CRD checks** confirm required operators have registered their custom resource definitions:

9. `DataScienceCluster` in `datasciencecluster.opendatahub.io/v1` (RHOAI operator)
10. `DSCInitialization` in `dscinitialization.opendatahub.io/v1` (RHOAI operator)
11. `ClusterPolicy` in `nvidia.com/v1` (GPU operator)
12. `NodeFeatureDiscovery` in `nfd.openshift.io/v1` (NFD operator)
13. `KnativeServing` in `operator.knative.dev/v1beta1` (OpenShift Serverless)
14. `ServiceMeshControlPlane` in `maistra.io/v2` (Service Mesh)

### Prerequisites

The in-cluster Kubernetes client must work from the pod (`rest.InClusterConfig()`). No special RBAC is needed. The default ServiceAccount is sufficient since all three check types use APIs that are available to any authenticated user: `SelfSubjectAccessReview` for permission checks, and the Discovery API for API group and CRD checks.

Note: operator health checks (e.g. verifying Deployment conditions, CR status fields, or DSC component conditions) were considered but would require cross-namespace read access to Deployments and custom resources, which conflicts with the sandbox design of this test. The CRD existence checks validate that operators are installed; actual operator health is validated implicitly by the downstream tests that depend on them.

### Design rationale

The permission checks are there to verify that the test pod is sandboxed. If any of them come back as allowed, the test fails because it means the ServiceAccount has more privileges than it should. This guards against accidentally running the test suite with an overly permissive ServiceAccount. The API group checks verify that the cluster has the CRDs that downstream tests depend on. For example, the llm-d test needs `inference.networking.k8s.io` for InferencePool resources. Finding out about a missing CRD here gives a much clearer error message than a cryptic failure partway through a later test. The `nonexistent.example.io` entry with expected `"no"` is a negative control that confirms the API group check logic is actually working and not just returning `"yes"` for everything. The CRD checks go one level deeper than API group checks by verifying that specific Kinds exist within an API group/version, which confirms the operator has registered its full CRD schema rather than just the API group being present.


---

## 2. platform-check-admin

### Purpose

This test is the admin counterpart to platform-check, ported from [memalhot/open-science-test-cases/operators/checks.sh](https://github.com/memalhot/open-science-test-cases/blob/main/operators/checks.sh). Where platform-check validates CRD existence using the Discovery API (available to any user), this test reads live operator Deployments, CR status fields, DSC component conditions, GPU node labels, and required pods across operator namespaces. These checks require cross-namespace and node-level read access, so the test runs under the `uat-sa` ServiceAccount in the `uat-admin` namespace with a ClusterRole.

### Architecture

```
checker [pass-fail, ephemeral, uat-admin namespace, uat-sa SA]
    reads operator Deployments across namespaces
    reads CR status via dynamic client
    reads node labels
    lists pods across namespaces
```

This is a single ephemeral pod with no GPUs, no server, no service, and no sweep phase.

### Infrastructure

| Name | Image | GPUs | Service | Key config |
|------|-------|------|---------|------------|
| checker | `registry.redhat.io/ubi9/ubi:9.8` | none | none | `ADMIN_CHECKS` env (JSON array of check objects), `serviceAccountName: uat-sa` |

The list of checks is defined in the test YAML and passed as a JSON array through the `ADMIN_CHECKS` environment variable. Five check types are supported, all returning `"yes"` or `"no"`:

| Check type | What it does | Fields |
|------------|-------------|--------|
| `deployment` | Checks if a Deployment has `Available: True` | `name`, `namespace` |
| `crField` | Checks a CR's `.status.<field>` against an expected value | `group`, `version`, `resource`, `field`, `value` |
| `crCondition` | Checks if a CR's condition has `status: True` | `group`, `version`, `resource`, `conditionType` |
| `nodeLabel` | Checks if all nodes matching a selector have a label (supports `\|` for OR) | `labelSelector`, `label` |
| `podsRunning` | Checks if >=1 pod is Running with a label selector in a namespace | `namespace`, `labelSelector` |

### Pass-fail assertions

**Operator deployment health** (5 checks):

1. `rhods-operator` in `redhat-ods-operator` is Available
2. `nfd-controller-manager` in `openshift-nfd` is Available
3. `gpu-operator` in `nvidia-gpu-operator` is Available
4. `knative-openshift` in `openshift-serverless` is Available
5. `istio-operator` in `openshift-operators` is Available

**Configuration readiness** (2 checks):

6. DataScienceCluster `.status.phase` is `Ready`
7. ClusterPolicy `.status.state` is `ready`

**CR condition checks** (11 checks):

8. NodeFeatureDiscovery condition `Available` is True
9. ServiceMeshControlPlane condition `Ready` is True
10. DSC component `DashboardReady` is True
11. DSC component `WorkbenchesReady` is True
12. DSC component `DataSciencePipelinesReady` is True
13. DSC component `CodeFlareReady` is True
14. DSC component `KserveReady` is True
15. DSC component `RayReady` is True
16. DSC component `TrustyAIReady` is True
17. DSC component `ModelMeshServingReady` is True
18. DSC component `KueueReady` is True

**GPU node labels** (2 checks):

19. All GPU nodes have NFD label (`feature.node.kubernetes.io/pci-10de.present` or `pci-0302_10de.present`)
20. All GPU nodes have `nvidia.com/gpu.count` label

**Required pods running** (2 checks):

21. NFD Worker pods running in `openshift-nfd` (`app=nfd-worker`)
22. NVIDIA GPU Driver pods running in `nvidia-gpu-operator` (`app.kubernetes.io/component=nvidia-driver`)

### Prerequisites

The `uat-sa` ServiceAccount must exist in the `uat-admin` namespace with a ClusterRole granting:

| Permission | Resources | Why |
|-----------|-----------|-----|
| `get` | `deployments` (in operator namespaces) | Operator health checks |
| `get`, `list` | `datascienceclusters`, `dscinitializations` | DSC phase and component conditions |
| `get`, `list` | `clusterpolicies` | GPU operator configuration readiness |
| `get`, `list` | `nodefeaturediscoveries` | NFD configuration readiness |
| `get`, `list` | `servicemeshcontrolplanes` | Service mesh health |
| `get`, `list` | `nodes` | GPU node label checks |
| `list` | `pods` (in operator namespaces) | Required pod checks |

This SA and ClusterRole are created by `setup/namespaces-and-pvcs.yaml`. The test must be run with the `cluster/ocp-test-admin.yaml` cluster config, which targets the `uat-admin` namespace.

### Design rationale

This test is separated from platform-check because the two have fundamentally different security models. platform-check runs as a regular sandboxed user and validates that the sandbox is properly locked down. This test runs with elevated permissions and validates things a sandboxed user cannot see. Keeping them separate means platform-check can continue to assert that the default SA has no cross-namespace access, while this test uses a purpose-built SA with the minimum ClusterRole needed. The check types use the Kubernetes typed client for Deployments, Nodes, and Pods, and the dynamic client for custom resources (DataScienceCluster, ClusterPolicy, etc.) since those types are not in the standard client-go API. The `nodeLabel` check supports `|` in the label field for OR semantics because NFD uses different PCI label names depending on the GPU's device class (`pci-10de` for display controllers, `pci-0302_10de` for 3D controllers).


---

## 3. component

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

## 4. iperf3

### Purpose

This test measures TCP bandwidth between every pair of nodes in the cluster using iperf3. It validates that the pod network can sustain the throughput needed for inter-node communication such as NCCL allreduce over TCP or KV cache transfers without RDMA.

### Architecture

```
iperf-server [persistent, node 0 of pair]
    |
    +--- iperf-client [pass-fail, ephemeral, node 1 of pair]
```

The test uses `permutation` placement with `setSize: 2`, so for N nodes it produces N*(N-1) test runs covering every ordered pair (A->B and B->A are separate tests). This catches asymmetric bandwidth issues caused by network topology or switch oversubscription.

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

The test uses `permutation` placement rather than `combination` because A->B and B->A are tested separately. Asymmetric bandwidth (due to network topology, switch oversubscription, or misconfigured QoS) would only show up if both directions are measured independently. The `-J` flag produces JSON output which is parsed directly for the `bits_per_second` value.


---

## 5. dev-env

### Purpose

This test deploys a Jupyter notebook server with all node GPUs and validates that the development environment is functional end-to-end. Rather than running diagnostics at startup, the validator pod creates a notebook on the server via the Jupyter Contents API, starts a kernel, executes CUDA diagnostic code through the Jupyter kernel websocket protocol, and then reads back the results. This proves the full interactive workflow works: notebook creation, kernel execution with GPU access, and file I/O through the Jupyter API.

### Architecture

```
notebook-server [persistent]
    |
    +--- validator [pass-fail, ephemeral]
             creates notebook via Contents API
             starts kernel, executes CUDA diagnostics via websocket
             reads result.txt via Contents API
             validates GPU count and NVLink against cluster spec
```

### Infrastructure

| Name | Image | GPUs | Service | Key config |
|------|-------|------|---------|------------|
| notebook-server | `quay.io/jschless/ml-dev-env:latest` | All node GPUs | headless ClusterIP :8888 | `jupyter lab --notebook-dir=/uat_workspace`, no auth, XSRF disabled, `HOME=/tmp` |
| validator | `registry.redhat.io/ubi9/ubi:9.8` | none | none | `SERVER_URL`, `EXPECTED_GPU_COUNT`, `EXPECTED_NVLINK` env |

### Pass-fail assertions

1. Notebook created on server via Jupyter Contents API (`PUT /api/contents/gpu-diagnostics.ipynb`)
2. Kernel started and diagnostic code executed via websocket (`/api/kernels/<id>/channels`)
3. `result.txt` readable via Contents API (`GET /api/contents/result.txt`)
4. GPU count from CUDA matches cluster spec (`nvidia.com/gpu`)
5. NVLink width from `nvidia-smi topo -m` matches cluster spec (`nvlink`)
6. All GPU devices reported by CUDA (device list length matches expected count)

### Prerequisites

The `quay.io/jschless/ml-dev-env:latest` image must be accessible from the cluster. At least one GPU must be available for the notebook server to schedule. The cluster spec must include `nvlink` in the node's `componentValidation.sanity` section.

### Design rationale

The validator uses a minimal websocket client built from Go's standard library to avoid adding external dependencies. The notebook server runs with `HOME=/tmp` because the image's root filesystem is not writable under OpenShift's arbitrary UID assignment. XSRF protection is disabled (`--ServerApp.disable_check_xsrf=True`) because the validator pod communicates with the Jupyter API programmatically without browser cookies. Authentication is disabled (`--IdentityProvider.token=`) since the server is only accessible within the cluster network via a headless ClusterIP service. The diagnostic code writes `result.txt` to `/uat_workspace` (the PVC mount) using an absolute path because the kernel's working directory defaults to the user's home, not the notebook directory.


---

## 6. kserve

### Purpose

This test validates that the KServe operator can deploy and serve a model through the InferenceService CRD. It creates an InferenceService in RawDeployment mode with vLLM as the model server, waits for the endpoint to become available, and then runs health, model, and inference checks against it. This confirms the operator stack is functional before relying on it for production workloads.

### Architecture

```
isvc [resourceConfig, InferenceService CRD]
    |
    +--- test-runner [pass-fail, ephemeral]
             polls endpoint until ready (10 min timeout)
             health / models / inference checks
```

The InferenceService is created as a resource step. KServe processes the CRD and creates a Deployment and Service in RawDeployment mode. The test runner pod polls the KServe-created service until the model server is healthy, then runs the assertions.

### Infrastructure

| Name | Image | GPUs | Service | Key config |
|------|-------|------|---------|------------|
| isvc | (KServe CRD, operator creates the pod) | 1 (configurable via `serverConfig.gpuCount`) | KServe-managed `{name}-predictor` :8080 | `--max-model-len=10000 --gpu-memory-utilization=0.6` |
| test-runner | `registry.redhat.io/ubi9/ubi:latest` | none | none | `SERVICE_URL`, `MODEL_NAME` env |

### Pass-fail assertions

1. Endpoint becomes available within 10 minutes (polls `/health` every 15s)
2. Model is loaded (`GET /v1/models` returns non-empty `data` array)
3. Inference request succeeds (`POST /v1/completions` returns valid response with choices)

### Prerequisites

The KServe operator must be installed and the `serving.kserve.io` API group must be available. The namespace must have RBAC permissions to create InferenceService resources. The models PVC must contain the model weights. At least one GPU must be available for the InferenceService pod to schedule.

### Design rationale

The test uses the `containers` predictor (inline container spec) rather than a separate ServingRuntime so the entire test is self-contained in a single CRD. The cluster defaults to RawDeployment mode, so no deployment mode annotation is needed. The test is restricted to project scope because the InferenceService CRD is a non-Pod resource whose spec is passed through as-is by the framework's generic `resource.yaml.j2` template. That template cannot inject a `nodeSelector` since the path differs by resource kind (`spec.predictor.nodeSelector` for InferenceService vs `spec.template.spec.nodeSelector` for Deployment, etc.). At node scope, two "per-node" InferenceServices could land on the same node, defeating the purpose. Node-scope support requires a `nodeSelectorPath` mechanism in the framework's resource step handling. The 10-minute polling timeout accounts for image pull time and model loading on first run.


---

## 7. guidellm

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

## 8. inference-perf

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

## 9. chunked-prefill

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

## 10. llm-d-local

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

## 11. ping

### Purpose

This test validates cross-namespace service connectivity by deploying simple HTTP servers in both the project and peer namespaces, then running connectivity checks from each side. A pod in the project namespace should be able to reach its own service but not the peer service by short name (namespace isolation). A pod in the peer namespace should be able to reach both its local peer service and the project service via cross-namespace FQDN.

### Architecture

```
project-server [persistent, project namespace]
    |
peer-server [persistent, peer namespace]
    |
    +--- project-check [pass-fail, ephemeral, project namespace]
    |        own service by short name ✓
    |        peer service by short name ✗
    |        peer service by FQDN ✗
    |
    +--- peer-check [pass-fail, ephemeral, peer namespace]
             own service by short name ✓
             project service by short name ✗
             project service by FQDN ✗
```

### Infrastructure

| Name | Image | GPUs | Service | Key config |
|------|-------|------|---------|------------|
| project-server | `registry.redhat.io/ubi9/ubi:latest` | none | headless ClusterIP :8080 | `python3 -m http.server 8080` |
| peer-server | `registry.redhat.io/ubi9/ubi:latest` | none | headless ClusterIP :8080 (peer namespace) | `python3 -m http.server 8080` |
| project-check | `registry.redhat.io/ubi9/ubi:latest` | none | none | `OWN_URL`, `OTHER_SHORT_URL`, `OTHER_FQDN_URL` env |
| peer-check | `registry.redhat.io/ubi9/ubi:latest` | none | none (peer namespace) | `OWN_URL`, `OTHER_SHORT_URL`, `OTHER_FQDN_URL` env |

### Pass-fail assertions

Both project-check and peer-check run the same three assertions with symmetric env vars:

1. HTTP GET to own service by short name returns 200 (service reachable in own namespace)
2. HTTP GET to other service by short name fails (DNS does not resolve across namespaces)
3. HTTP GET to other service by FQDN fails (network policy blocks cross-namespace traffic)

### Prerequisites

The peer namespace must be configured in the cluster spec (`peerNamespace`). Both namespaces must have the RBAC permissions to create pods and services. The `serverConfig.projectNamespace` and `serverConfig.peerNamespace` must match the cluster spec.

### Design rationale

The test uses Python's built-in `http.server` module rather than nginx because it runs as non-root without any configuration, which works with OpenShift's default security context. Both checks run identical assertions with symmetric env vars: own service reachable, other service unreachable by short name (DNS scoping), other service unreachable by FQDN (network policy). This validates both DNS namespace isolation and network-level cross-namespace blocking.


---

## 12. peer-load-high

### Purpose

This test measures inference performance under high cross-namespace GPU contention. It deploys tensor-parallel vLLM servers with chunked prefill in both the project and peer namespaces on the same node, splitting the node's GPUs evenly between them (peer gets g/2, project gets g - g/2). It starts a sustained high-rate load generator (10 req/s for 10 minutes) against the peer server, then runs benchmark sweeps against the project server. This noisy-neighbor pattern reveals how inference latency and throughput degrade when a co-located multi-GPU workload is under heavy load.

### Architecture

```
peer-server [persistent, peer namespace]
    |
    +--- peer-load [persistent, peer namespace, 10 req/s × 600s]

project-server [persistent, project namespace]
    |
    +--- sweep [ephemeral, one pod per entry]
             constant-low
             constant-high
             poisson-burst
```

Both servers are pinned to the same node via nodeSelector (node scope only). The peer-load pod starts before the sweep and runs continuously throughout.

### Infrastructure

| Name | Image | GPUs | Service | Key config |
|------|-------|------|---------|------------|
| peer-server | `nvcr.io/nvidia/vllm:26.03-py3` | g/2 | headless ClusterIP :8000 (peer namespace) | `--tensor-parallel-size=<g/2> --enable-chunked-prefill` |
| project-server | `nvcr.io/nvidia/vllm:26.03-py3` | g - g/2 | headless ClusterIP :8000 | `--tensor-parallel-size=<g - g/2> --enable-chunked-prefill` |
| peer-load | `quay.io/inference-perf/inference-perf:v0.6.1` | none | none (peer namespace) | constant 10 req/s, 600s duration, 4 workers |
| sweep | `quay.io/inference-perf/inference-perf:v0.6.1` | none | none | `SERVER_URL`, `SWEEP_COMMAND` env |

### Sweep entries

| ID | Load type | Rate | Duration | Workers | Data |
|----|-----------|------|----------|---------|------|
| constant-low | constant | 1 req/s | 30s | 4 | random (input: 64-256, mean=128; output: 32-128, mean=64) |
| constant-high | constant | 10 req/s | 60s | 4 | same |
| poisson-burst | poisson | 5 req/s | 60s | 4 | same |

Results are quantitative only (no pass-fail assertions). Throughput (total and output tokens/s), request latency, TTFT, and TPOT are reported via Ginkgo `AddReportEntry`.

### Prerequisites

All GPUs on the target node must be available since peer-server takes g/2 and project-server takes the remaining g - g/2. The peer namespace must be configured in the cluster spec. The models PVC must be mounted at `/models` in both namespaces.

### Design rationale

The peer-load pod has no readinessProbe so it becomes Ready immediately after starting, which ensures the background load is running before the sweep pods launch. The 10 req/s rate with 4 workers creates sustained GPU pressure on the peer server without saturating it to the point of request failures. Node scope is required so both servers land on the same physical node, creating real GPU contention through shared PCIe bandwidth, NVLink fabric, memory controller, and power delivery. The GPU split uses `g // 2` for peer and `g - (g // 2)` for project, which on an odd GPU count gives the project server the extra GPU. Tensor parallelism with chunked prefill is enabled on both servers so each uses all its allocated GPUs in a single TP group, matching production-like multi-GPU serving configurations. Comparing these results against the chunked-prefill baseline (test 9) quantifies the performance impact of noisy-neighbor GPU workloads.


---

## 13. peer-load-low

### Purpose

This test is identical to peer-load-high but with a low-rate background load (1 req/s instead of 10 req/s) on the peer server. It provides a low-contention data point that, when compared against peer-load-high and the chunked-prefill baseline, reveals the relationship between neighbor load intensity and inference performance degradation.

### Architecture

```
peer-server [persistent, peer namespace]
    |
    +--- peer-load [persistent, peer namespace, 1 req/s × 600s]

project-server [persistent, project namespace]
    |
    +--- sweep [ephemeral, one pod per entry]
             constant-low
             constant-high
             poisson-burst
```

### Infrastructure

| Name | Image | GPUs | Service | Key config |
|------|-------|------|---------|------------|
| peer-server | `nvcr.io/nvidia/vllm:26.03-py3` | g/2 | headless ClusterIP :8000 (peer namespace) | `--tensor-parallel-size=<g/2> --enable-chunked-prefill` |
| project-server | `nvcr.io/nvidia/vllm:26.03-py3` | g - g/2 | headless ClusterIP :8000 | `--tensor-parallel-size=<g - g/2> --enable-chunked-prefill` |
| peer-load | `quay.io/inference-perf/inference-perf:v0.6.1` | none | none (peer namespace) | constant 1 req/s, 600s duration, 4 workers |
| sweep | `quay.io/inference-perf/inference-perf:v0.6.1` | none | none | `SERVER_URL`, `SWEEP_COMMAND` env |

### Sweep entries

Same as peer-load-high. See [peer-load-high sweep entries](#12-peer-load-high).

### Prerequisites

Same as peer-load-high. See [peer-load-high prerequisites](#12-peer-load-high).

### Design rationale

At 1 req/s the peer server processes requests with minimal queuing, so the GPUs are mostly idle between requests. Any performance degradation observed in the project-side sweep at this load level points to fixed overhead from co-tenancy (shared NVLink fabric, memory controller, PCIe bus arbitration, GPU context switching) rather than sustained compute contention. This makes peer-load-low the control for peer-load-high's experiment.
