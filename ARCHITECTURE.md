<!-- Assisted by Claude Opus 4.6 -->
# UAT Test Harness — Architecture

## Overview

A declarative test harness that generates Kubernetes manifests from test definitions. Given a list of target nodes, the harness produces manually-executable manifests as the primary output, then derives Tekton pipeline manifests from them. Tests are organized into three scopes: **node** (per-node, pinned via nodeSelector), **cluster** (multi-node coordinated), and **project** (namespace-level, no node pinning).

```
                                                          ┌→ Manual Manifests
Test Definitions (YAML + Go) + Node List → python -m src ─┤                     → OpenShift Execution → Results on PVC
                                                          └→ Tekton Manifests
```

## Input Format

Adding a test to the suite requires three things:

1. **An entry in `test_suite.yaml`** — the suite-level manifest that lists tests by scope (`node`, `cluster`, `project`) in execution order, plus global execution settings (e.g. stop-on-failure policy). Cluster-level settings (storage, timeouts) live in the cluster config.

   ```yaml
   spec:
     tests:
       node:
         - component
         - inference
       cluster: []
       project: []
   ```

2. **`<test>.yaml`** — the test definition containing:
   - **DAG**: ordered resource graph (e.g. deploy a vLLM server, then run a test pod). Each vertex declares its image, command, env, ports, probes, and whether it persists through the parameter sweep or runs once per sweep iteration. Non-persistent steps may include a `parameterSweep` — a list of named entries, each with an `id`, `description`, and `command` list. The generator produces a separate test pod for each sweep entry.
   - **Server config**: template variables substituted into DAG commands (model name, memory settings, etc.).

3. **`<test>.go`** — a Ginkgo test file implementing the test logic. A single compiled binary handles all parameter sweep entries — each sweep entry runs as a separate pod with per-entry env vars.

## Generation

The generator takes the YAML definitions, accompanying Go files, and a list of target nodes as input. It uses a three-layer architecture:

1. **Step computation** — converts test definitions into a flat, ordered list of steps. Each step corresponds to one Kubernetes resource or shell action and captures the pod name, manifest content, wait conditions, and ordering. Both output layers consume the same step list — they must never diverge.

2. **Manual writer** — writes the steps as standalone files to `build/manual/`, organized by phase (setup, per-node, teardown). These are the primary output: each `.yaml` is applied with `oc apply -f`, each `.sh` is run directly.

3. **Tekton writer** — derives Tekton Tasks and Pipelines from the same steps. Pod manifests are embedded directly in Tekton Task scripts, so `build/tekton/` is self-contained.

Every rendered manifest must be validated at generation time — invalid YAML, missing `apiVersion`, `kind`, or `metadata.name` must fail the generator immediately rather than producing broken manifests that only surface at `oc apply` time.

### Output Structure

```
build/
├── manual/
│   ├── setup/
│   │   ├── configmap.yaml
│   │   ├── builder-pod.yaml
│   │   └── build.sh
│   ├── nodes/
│   │   ├── <node>/
│   │   │   ├── 01-<step>.yaml
│   │   │   ├── 02-cleanup-<step>.sh
│   │   │   ├── ...
│   │   │   └── NN-teardown-<test>.sh
│   │   └── (one directory per node)
│   └── teardown/
│       ├── aggregator-pod.yaml
│       ├── aggregate.sh
│       └── cleanup.sh
└── tekton/
    ├── configmap.yaml
    ├── cluster-pipeline.yaml
    ├── node-pipeline-<node>.yaml  (one per node)
    ├── task-*.yaml
    └── pipelinerun.yaml
```

Steps suffixed `.yaml` are applied with `oc apply -f`. Steps suffixed `.sh` are shell scripts run directly (`bash <script>`). Within each node directory, files are numbered in execution order.

Pod names are prefixed with the node name (e.g. `<node>-vllm-server`) to avoid Kubernetes name collisions when running multiple nodes in parallel. Service URL references in env vars and commands are automatically rewritten to match.

## Execution

Execution is split across two levels of Tekton Pipelines, with cluster and project tests running after node pipelines complete.

### Cluster Pipeline

```
create-builder → build-binaries → [node1-pipeline, node2-pipeline, ...] → cluster-tests → project-tests → aggregate → cleanup
                                   (parallel, non-blocking)                                                  (finally)
```

**1. Create builder pod** — a long-lived Go toolchain pod with the PVC mounted at `/workspace` and the ConfigMap mounted at `/src/`.

**2. Build binaries** — copies source from ConfigMap mounts into the PVC, compiles one Ginkgo binary per test at `/workspace/binaries/<test>/test.bin`.

**3. Node pipelines** — references each node pipeline via `pipelineRef` (Tekton Pipelines in Pipelines). All node pipelines run in parallel. Each shares the PVC: binaries are read-only, results write to node-specific directories, so there are no race conditions.

**4. Cluster tests** — multi-node coordinated tests (e.g. RDMA perftest). The cluster pipeline orchestrates tasks across nodes directly — no per-node pipeline. For example, a network bandwidth test might place a server on node A and a client on node B, then collect results.

**5. Project tests** — namespace-level tests with no node affinity. These validate project-wide concerns (quotas, RBAC, network policies) that don't require specific node placement. Run after cluster tests since they may depend on cluster-level state.

**6. Aggregate results (finally)** — spins up a separate aggregator pod with the PVC mounted. Reads individual JUnit/JSON reports and generates a consolidated report. Runs after all pipelines and tests complete (success or failure). Must complete before cleanup.

**7. Cleanup (finally)** — deletes the builder and aggregator pods. Ordered after aggregation within the finally block (Tekton finally tasks run in parallel by default, so explicit ordering is required).

### Node Pipeline

Each node pipeline runs the full node-scoped test suite on its target node, pinned via `nodeSelector`. Each step gets its own Tekton task, with the pod manifest embedded directly in the task script.

```
run-component-test-runner → cleanup-component-test-runner
  → deploy-inference-dag
  → run-inference-pass-fail → cleanup-inference-pass-fail
  → run-inference-sweep → cleanup-inference-sweep
  → teardown-inference
  → (finally) teardown-inference
```

For each test:
  - Deploy DAG resources marked `persistsThroughSweep` (e.g. a vLLM inference server with its Service).
  - Wait for readiness.
  - For each non-persistent DAG step, apply a test pod and wait for completion. If the step has a `parameterSweep`, one pod is created per entry. Results write to the PVC. After each ephemeral pod completes, a cleanup step deletes it (filtered by `test=<name>,node=<node>,step=<step_id>` labels) to release resources like GPUs for subsequent steps.
  - Tear down the persistent DAG resources for this test (filtered by `test=<name>,node=<node>` labels).

DAG teardown also runs in the node pipeline's `finally` block so that resources (e.g. GPU-backed deployments) are cleaned up even if a step fails.

## Results

Each test run writes JUnit XML and benchmark output to the PVC in a hierarchical directory structure, organized by scope, node, test, and DAG step:

```
<base-path>/<pipeline-run-name>/
├── binaries/
│   ├── component/test.bin
│   └── inference/test.bin
├── node/
│   ├── wrk-4/
│   │   ├── component/
│   │   │   └── test-runner/
│   │   │       └── junit.xml
│   │   └── inference/
│   │       ├── vllm-server/              # persistent DAG pod workspace
│   │       ├── pass-fail/
│   │       │   └── junit.xml
│   │       ├── short-burst/              # sweep entry
│   │       │   ├── junit.xml
│   │       │   └── results.json
│   │       └── sustained-load/
│   │           └── junit.xml
│   └── wrk-6/
│       └── ...
├── cluster/                              (future)
├── project/                              (future)
└── report/
    └── summary.json
```

The base path is a cluster-level setting that scopes results to a particular test suite or environment (e.g. `uat/results`). The pipeline run name provides timestamp-based isolation between runs. Every DAG step gets a unique directory computed from its position in the hierarchy (`<scope>/<node>/<test>/<dag_step>/`). Test pods write to `/workspace` and files land in the right place via Kubernetes `subPath` mounting. The aggregator scans `node/`, `cluster/`, and `project/` subdirectories for `junit.xml` files and writes a consolidated summary to `report/`.

## Design Decisions

| Decision | Rationale |
|---|---|
| Manual-first output | Standalone pod manifests are the primary output. Tekton manifests are derived from them, embedding the same pod YAML in task scripts. This makes the manifests debuggable outside Tekton and ensures manual and automated paths test the same resources. |
| Three test scopes | **Node** tests validate per-node hardware (GPUs, drivers). **Cluster** tests validate multi-node coordination (RDMA, interconnect). **Project** tests validate namespace-level concerns (quotas, RBAC) without node affinity. Currently only node scope is implemented; cluster and project are planned. |
| Node-prefixed pod names | Parallel node pipelines share a namespace. Prefixing pod and service names with the node name (e.g. `wrk-6-vllm-server`) prevents collisions. Service URL references are rewritten automatically. |
| One binary per test, not per parameter | Same test logic, different runtime config. Avoids redundant compilation. |
| ConfigMap → Builder Pod → PVC | A single ConfigMap delivers all Go source to the builder pod. Builder pod provides a persistent compilation environment. PVC makes binaries accessible to any test container. Delivery mechanism is swappable (GitHub pull, custom image) without changing the rest of the pipeline. |
| DAG resources persist through sweep | Expensive resources (GPU-backed servers) deploy once; the parameter sweep reuses them. |
| One Tekton task per DAG step | Each non-persistent step gets its own task (not one per test). Sweep iterations each get a separate test pod and task, keeping the Tekton task graph explicit. |
| Ephemeral pod cleanup after each step | Non-persistent pods are deleted immediately after completion to release resources (e.g. GPUs) for subsequent steps. Each ephemeral pod carries a `step` label for targeted deletion without affecting persistent pods. |
| Cluster pipeline / node pipeline split | Separates build (once) from execution (per node). Node pipelines scale with cluster size and run in parallel. A single cluster pipeline manages shared resources (builder pod, aggregation). |

## Constraints

- **One cluster pipeline per namespace**: the builder pod has a fixed name, so only one cluster pipeline can run at a time in a given namespace. This is typically sufficient — the node pipelines are the element that scales with cluster size, and a single cluster pipeline fans out to all target nodes in parallel.
- **Sequential sweeps**: parameter sweep entries within a test run as separate pods in sequence — a failure in one aborts the rest of that test's sweep on that node.
