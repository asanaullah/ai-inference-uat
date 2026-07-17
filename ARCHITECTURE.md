<!-- Assisted by Claude Opus 4.6 -->
# UAT Test Harness — Architecture

## Overview

A declarative test harness that generates Kubernetes manifests from test definitions. Given a list of target nodes, the harness computes a flat, ordered list of steps, then independently derives both manually-executable manifests and Tekton pipeline manifests from that same step list. Tests are listed in execution order, each specifying a scope (**node**, **cluster**, or **project**) and a per-test failure policy.

```
                                                                    ┌→ Manual Manifests
Test Definitions (YAML + Go) + Node List → python -m src → Steps ──┤                     → OpenShift Execution → Results on PVC
                                              ↑                     └→ Tekton Manifests
                                              │
                                       steps.json (optional re-entry point)
```

After step computation, the generator validates pod and service names, assigns `onError` to command steps (setup steps get `stopAndFail`, global finally steps get `continue`, test steps derive from the test's `onFailure` policy, and per-test finally steps get no `onError` since Tekton `finally` always runs), and serializes the step list to `steps.json`. This file can be fed back to the generator via `--steps` to regenerate manual and Tekton output without re-reading test definitions — useful for editing steps externally or re-running writers with different options. When loading from `steps.json`, the generator re-validates structure, pod and service names, and `onError` correctness.

## Input Format

Adding a test to the suite requires three things:

1. **An entry in `test_suite.yaml`** — the suite-level manifest that lists tests in execution order. Each entry specifies the test name, scope (`node`, `cluster`, or `project`), what to do on failure, and an optional per-test timeout. Storage settings (PVC, base path) live in the cluster config. Default timeouts and tool images live in `config.yaml`.

   ```yaml
   spec:
     tests:
       - name: component
         scope: node
         onFailure: continue

       - name: inference
         scope: node
         onFailure: abort
         timeout: 1200s
   ```

   The `onFailure` field controls what happens when a step within the test fails (default: `continue`):
   - `continue` — continue executing remaining steps within this test before proceeding to the next test.
   - `skipTest` — skip remaining steps within this test (tear down its resources), proceed to the next test.
   - `abort` — abort the entire suite immediately.

   The optional `timeout` field overrides the cluster-level `defaultTestTimeout` for this test's ephemeral pods. If omitted, the default from `config.yaml` is used.

2. **`<test>.yaml`** — the test definition containing:
   - **DAG**: ordered resource graph (e.g. deploy a vLLM server, then run a test pod). Each vertex declares its image, command, env, ports, probes, resources, volume mounts, an optional service, and whether it persists through the parameter sweep or runs once per sweep iteration. Vertices may also specify a Ginkgo label filter (as an alternative to an explicit command), privileged mode, and extra volumes. Non-persistent steps may include a `parameterSweep` — a base command and a list of named entries, each with an `id`, `description`, and `flags` that are merged over the base command's flags. The generator produces a separate test pod for each sweep entry.
   - **Server config**: template variables substituted into DAG commands (model name, memory settings, etc.).

3. **`<test>.go`** — a Ginkgo test file implementing the test logic. A single compiled binary handles all parameter sweep entries — each sweep entry runs as a separate pod with per-entry command flags and workspace directory.

## Generation

The generator takes the YAML definitions, accompanying Go files, and a list of target nodes as input. It uses a three-layer architecture:

1. **Step computation** — converts test definitions into a flat, ordered list of steps. Each step is either a resource to create (pod manifest, optionally bundled with a service) or an action to execute (apply a manifest, exec into a pod, delete resources). Ordering is implicit in list position. Both output layers consume the same step list.

2. **Manual writer** — writes the steps as standalone files to `build/manual/`, organized by phase (setup, test, teardown). These are the primary output: numbered `.sh` scripts in `manual/` are what the operator runs in order. Manifests (`.yaml`) are written to `manual/manifests/` as data files — each apply script references its manifest via `oc apply -f manifests/<name>.yaml`.

3. **Tekton writer** — derives Tekton Tasks and Pipelines from the same steps. Pod manifests are embedded directly in Tekton Task scripts, so `build/tekton/` is self-contained.

Every rendered manifest must be validated at generation time — invalid YAML, missing `apiVersion`, `kind`, or `metadata.name`/`metadata.generateName` must fail the generator immediately rather than producing broken manifests that only surface at `oc apply` time. Pod names are validated for RFC 1123 label compliance (lowercase alphanumeric, hyphens, etc.) and uniqueness after computation — a duplicate would cause resource collisions. Service names are validated for DNS-1035 compliance (must start with a lowercase letter, contain only lowercase alphanumeric characters and hyphens, and end with a lowercase alphanumeric character).

Each step carries two names: a human-readable **step name** (used for manual script filenames, PVC directory paths, and Tekton filenames on disk) and a **resource name** (used for Kubernetes `metadata.name` on pods, services, and Tekton tasks). The resource name substitutes a sanitized version of the node name: invalid characters (dots, underscores, etc.) are replaced with dashes, uppercase is lowercased, and names longer than 16 characters are truncated to 12 characters with a 4-character hash suffix. When the node name is short and already RFC 1123 compliant (e.g. `wrk-4`), both names are identical.

### Output Structure

```
build/
├── manual/
│   ├── manifests/
│   │   ├── apply-configmap.yaml                         ← setup manifest
│   │   ├── create-builder.yaml                          ← setup manifest
│   │   ├── 1-component-wrk-4-test-runner.yaml           ← test manifest
│   │   ├── 1-component-wrk-6-test-runner.yaml
│   │   ├── 2-inference-wrk-4-vllm-server.yaml           ← persistent DAG manifest
│   │   ├── 2-inference-wrk-6-vllm-server.yaml
│   │   ├── 2-inference-wrk-4-pass-fail.yaml             ← sweep entry manifest
│   │   ├── 2-inference-wrk-6-pass-fail.yaml
│   │   ├── ...
│   │   └── create-aggregator.yaml                       ← teardown manifest
│   ├── 01-apply-configmap.sh                            ← apply script
│   ├── 02-create-builder.sh                             ← apply script
│   ├── 03-build.sh                                      ← exec script
│   ├── 04-1-component-wrk-4-test-runner.sh              ← apply script (parallel nodes share counter)
│   ├── 04-1-component-wrk-6-test-runner.sh
│   ├── ...
│   ├── 07-2-inference-wrk-4-vllm-server.sh              ← apply script
│   ├── 07-2-inference-wrk-6-vllm-server.sh
│   ├── 08-2-inference-wrk-4-pass-fail.sh                ← apply script
│   ├── 08-2-inference-wrk-6-pass-fail.sh
│   ├── ...
│   ├── NN-create-aggregator.sh                          ← apply script
│   ├── N-aggregate.sh                                   ← exec script
│   └── N-cleanup.sh                                     ← delete-all script
└── tekton/
    ├── cluster-pipeline.yaml
    ├── node-1-component-wrk-4.yaml              (node-scoped: one per node × test)
    ├── node-2-inference-wrk-4.yaml
    ├── test-1-component-wrk-4.yaml              (node-scoped: one per node × test)
    ├── test-2-inference-wrk-4.yaml
    ├── test-3-network.yaml                      (cluster/project-scoped: one per test, planned)
    ├── task-*.yaml
    └── pipelinerun.yaml
```

Manifests (`.yaml`) are written to `manual/manifests/` without a counter prefix — they are data files, not actions. Numbered shell scripts (`.sh`) are written to `manual/` and are what the operator runs in order: apply scripts reference the corresponding manifest (`oc apply -f manifests/<name>.yaml`), exec scripts run commands, and delete scripts clean up resources. Steps that run in parallel across nodes share the same counter. The counter is zero-padded to the width of the total step count so that shell glob ordering (`*.sh`) matches execution order. The numbered scripts are the single source of "what to do, in what order."

`<test_id>` is the 1-indexed position of the test in the `test_suite.yaml` list (not zero-padded). The same test can appear multiple times in the list (e.g. with different configs or failure policies), so `<test_id>` prevents collisions in resource names and results paths, while `<test_name>` provides readability. For node-scoped tests, `<node>` is added to prevent collisions across parallel nodes. Service names are prefixed with `svc-` for DNS-1035 compliance (services require names starting with a letter). Service URL references in env vars and commands are automatically rewritten to match.

## Execution

Execution uses three levels of Tekton Pipelines: **cluster**, **node**, and **test**. Every test gets its own test pipeline with a `finally` block for cleanup. Node-scoped tests each get a node pipeline per node (one node pipeline per test × node combination, not one per node). The cluster pipeline sequences tests in `test_suite.yaml` list order, referencing node pipelines for node-scoped tests and test pipelines directly for cluster/project-scoped tests.

### Cluster Pipeline

```
apply-configmap → create-builder → build → [tests in list order] → finally: create-aggregator → aggregate → cleanup
                                                                            (sequenced via runAfter)
```

**1. Apply ConfigMap** — creates a ConfigMap containing all Go source, cluster config, test suite config, build script, and aggregator script.

**2. Create builder pod** — a long-lived Go toolchain pod with the PVC mounted at `/workspace` and the ConfigMap mounted at `/src/`.

**3. Build binaries** — copies source from ConfigMap mounts into the PVC, generates a `go.mod` with the Ginkgo version pinned in `config.yaml`, and compiles one Ginkgo binary per unique test name at `/workspace/<test>/test.bin`. If the same test name appears multiple times in `test_suite.yaml` (e.g. with different failure policies), all instances share the same binary.

**4. Tests** — executed in `test_suite.yaml` list order. Each test produces one pipeline entry in the cluster pipeline. Scope determines the shape:

- **Node** tests produce one node pipeline per target node, all running in parallel (no `runAfter` between nodes for the same test). Each node pipeline wraps a single test pipeline via `pipelineRef`. The next test in the list waits for all node pipelines of the previous test to complete. If a test fails on one node, the other nodes' pipelines for that test still run to completion (Tekton does not cancel in-flight parallel tasks). What happens next depends on `onFailure`: with `abort`, the node pipeline references have `onError: stopAndFail`, so a failure on *any single node* is enough to stop the cluster pipeline — the successes on other nodes do not override it. With `continue` or `skipTest`, the references have `onError: continue`, so failures on any or all nodes are ignored and the cluster pipeline proceeds to the next test.
- **Cluster** tests produce a single test pipeline directly referenced in the cluster pipeline. They orchestrate tasks across nodes — for example, placing a server on node A and a client on node B, then collecting results. *(not yet implemented — step computation returns an empty list, and the Tekton writer rejects any non-node-scoped test steps with a hard error)*
- **Project** tests produce a single test pipeline directly referenced in the cluster pipeline, without node affinity. They validate project-wide concerns (quotas, RBAC, network policies). *(not yet implemented — same as cluster)*

Because each test is a separate entry in the cluster pipeline, scopes can be freely interleaved (e.g. node test → cluster test → node test) without any grouping constraints.

**5. Aggregate results (finally)** — creates an aggregator pod, then execs into it to read individual JUnit/JSON reports and generate a consolidated report. Runs after all tests complete (success or failure). Must complete before cleanup.

**6. Cleanup (finally)** — deletes all pods, services, and deployments matching the managed-by label, and the ConfigMap. Ordered after aggregation within the finally block (Tekton finally tasks run in parallel by default, so explicit ordering is required).

### Node Pipeline

Each node pipeline runs exactly one test on one node. It contains a single `pipelineRef` to the test pipeline. Pods within the test are pinned to the target node via `nodeSelector` in the pod manifests. There is one node pipeline per (node × node-scoped test) combination.

```
node-1-component-wrk-4:
  test-1-component-wrk-4 (pipelineRef → test-1-component-wrk-4)

node-2-inference-wrk-4:
  test-2-inference-wrk-4 (pipelineRef → test-2-inference-wrk-4)
```

### Test Pipeline

Each test is its own pipeline. Its `finally` block contains a `finally-teardown` step that cleans up all the test's resources, hardcoded to always run so that resources are freed even if a step fails and `onError` stops execution.

DAG steps are processed in the order they appear in the test definition. Persistent and ephemeral steps can be interleaved. For each DAG step:

- **Persistent** (`persistsThroughSweep: true`): deploy the pod (and optional Service), wait for readiness. The resource stays up for subsequent steps to use.
- **Ephemeral** (`persistsThroughSweep: false`, the default): apply a test pod (and optional Service) and wait for completion. If the step has a `parameterSweep`, one pod is created per entry. Results write to the PVC. After each ephemeral pod completes, a cleanup step deletes pods, services, and deployments matching `test=<name>,node=<node>,sweep=<sweep_id>` labels to release resources like GPUs for subsequent steps.

After all DAG steps complete, persistent resources are torn down — deleting pods, services, and deployments filtered by `test=<name>,node=<node>` labels. `finally-teardown` runs regardless of success or failure, cleaning up all remaining resources for the test — both persistent and ephemeral — using the same label filter and resource types.

```
test-2-inference-wrk-4:
  tasks: 2-inference-wrk-4-vllm-server
    → 2-inference-wrk-4-pass-fail → 2-inference-wrk-4-cleanup-pass-fail
    → 2-inference-wrk-4-sweep-short-burst → 2-inference-wrk-4-cleanup-sweep-short-burst
    → ...
    → 2-inference-wrk-4-teardown
  finally: 2-inference-wrk-4-finally-teardown
```

## Results

Each test run writes JUnit XML and benchmark output to the PVC in a flat directory structure. Each step's workspace directory is named after its step name — the same name used for pod names, filenames, and Tekton task names:

```
<base-path>/<pipeline-run-name>/
├── binaries/
│   ├── component/test.bin
│   └── inference/test.bin
├── 1-component-wrk-4-test-runner/
│   └── junit.xml
├── 1-component-wrk-6-test-runner/
│   └── junit.xml
├── 2-inference-wrk-4-vllm-server/             # persistent DAG pod workspace
├── 2-inference-wrk-4-pass-fail/
│   └── junit.xml
├── 2-inference-wrk-4-sweep-short-burst/
│   ├── junit.xml
│   └── results.json
├── 2-inference-wrk-4-sweep-sustained-load/
│   └── junit.xml
├── 2-inference-wrk-6-vllm-server/
├── 2-inference-wrk-6-pass-fail/
│   └── junit.xml
├── ...
├── 3-network-client/                          # cluster-scoped (no node segment, planned)
│   └── junit.xml
└── report/
    └── summary.json
```

The base path is a cluster-level setting that scopes results to a particular test suite or environment (e.g. `uat/results`). The pipeline run name provides timestamp-based isolation between runs. Each step gets a flat directory named after its step name, which encodes the test index, test name, node (for node-scoped tests), and DAG step for uniqueness and readability. Test pods write to `/workspace` and files land in the right place via Kubernetes `subPath` mounting. The aggregator scans for `junit.xml` files across all step directories and writes a consolidated summary to `report/`.

## Design Decisions

| Decision | Rationale |
|---|---|
| Steps-first generation | The generator computes a flat, ordered step list from test definitions, then both the manual writer and the Tekton writer independently derive their output from that same list. This ensures both paths always produce equivalent resources, and makes it straightforward to add writers for other orchestration harnesses without changing step computation. |
| Three test scopes, one list | **Node** tests validate per-node hardware (GPUs, drivers). **Cluster** tests validate multi-node coordination (RDMA, interconnect). **Project** tests validate namespace-level concerns (quotas, RBAC) without node affinity. All three scopes are declared in a single ordered list in `test_suite.yaml`, allowing interleaved execution — each test is its own pipeline entry in the cluster pipeline, so scopes can alternate freely. Currently only node scope is implemented; cluster and project are planned. |
| Unified step naming | DAG steps follow a single naming convention: `<test_id>-<test>-<node>-<dag_step>` (node-scoped) or `<test_id>-<test>-<dag_step>` (cluster/project-scoped), with `-<id>` appended for sweep entries. Each step carries a human-readable **step name** (used for filenames and PVC paths) and a **resource name** (used for Kubernetes `metadata.name` on pods, services, and Tekton tasks). The resource name uses a sanitized node name where invalid characters are replaced with dashes and names over 16 characters are truncated to 12 + a 4-character hash. When the node name is short and RFC 1123 compliant, both names are identical. Lifecycle steps extend the convention with a fixed suffix: `<prefix>-cleanup-<dag_step>[-<id>]` (per-ephemeral-step cleanup), `<prefix>-teardown` (persistent resource teardown), and `<prefix>-finally-teardown` (always-run safety net). `<test_id>` prevents collisions when the same test appears multiple times in the suite; `<node>` prevents collisions across parallel nodes. Service names are prefixed with `svc-` for DNS-1035 compliance. Service URL references are rewritten automatically. |
| One binary per test, not per parameter | Same test logic, different runtime config. Avoids redundant compilation. |
| ConfigMap → Builder Pod → PVC | A single ConfigMap delivers all Go source to the builder pod. Builder pod provides a persistent compilation environment. PVC makes binaries accessible to any test container. Delivery mechanism is swappable (GitHub pull, custom image) without changing the rest of the pipeline. |
| DAG resources persist through sweep | Expensive resources (GPU-backed servers) deploy once; the parameter sweep reuses them. |
| One Tekton task per DAG step | Each non-persistent step gets its own task (not one per test). Sweep iterations each get a separate test pod and task, keeping the Tekton task graph explicit. |
| Ephemeral pod cleanup after each step | Non-persistent pods are deleted immediately after completion to release resources (e.g. GPUs) for subsequent steps. Each ephemeral step's pod and service carry a `sweep` label for targeted deletion without affecting persistent resources. |
| One node pipeline per test × node | Each node-scoped test gets its own node pipeline per node (not one node pipeline grouping all tests). This keeps the cluster pipeline's test ordering flat — each test is a separate entry, and node pipelines for the same test run in parallel while different tests run in sequence. A single cluster pipeline manages shared resources (builder pod, aggregation). |
| Per-test failure policy | Each test declares its own `onFailure` (`continue`, `skipTest`, `abort`) instead of a single global flag. Each test is wrapped in its own pipeline (Pipeline-in-Pipeline). For node-scoped tests, `onFailure` controls behavior at two levels: **inner** (test pipeline step-level `onError`) and **outer** (node pipeline's `pipelineRef` to the test pipeline). Cluster/project-scoped tests only have the inner level, since their test pipeline is referenced directly in the cluster pipeline. `continue`: inner steps get `onError: continue`, outer gets `onError: continue`. `skipTest`: inner steps get `onError: stopAndFail`, outer gets `onError: continue` — the test stops on first failure, cleans up via `finally`, and the cluster pipeline proceeds to the next test. `abort`: inner steps get `onError: stopAndFail`, outer gets `onError: stopAndFail` — the cluster pipeline halts. The test pipeline's `finally` block always runs cleanup regardless of errors. |

## Constraints

- **ConfigMap 1MB limit**: all Go source, cluster config, test suite config, build script, and aggregator script are packed into a single ConfigMap. A project with many tests may exceed Kubernetes' 1MB ConfigMap limit.
- **Resource name length**: resource names are constructed by concatenating test_id, test name, sanitized node name, and DAG step (e.g. `2-inference-wrk-4-vllm-server`, `node-2-inference-wrk-4`). Node names are capped at 16 characters (12 + 4-char hash if longer), but the full resource name can still exceed the 63-character Kubernetes name limit with long test or DAG step names.
- **One cluster pipeline per namespace**: the builder pod has a fixed name, so only one cluster pipeline can run at a time in a given namespace. This is typically sufficient — the node pipelines are the element that scales with cluster size, and a single cluster pipeline fans out to all target nodes in parallel.
- **Sequential sweeps**: parameter sweep entries within a test run as separate pods in sequence. Failure behavior is controlled per-test via the `onFailure` field in `test_suite.yaml` (`continue`, `skipTest`, or `abort`). Each test is its own pipeline: `continue` sets `onError: continue` on inner steps so the test runs through failures; `skipTest` sets `onError: stopAndFail` on inner steps so the test stops on first failure but the cluster pipeline proceeds to the next test; `abort` sets `onError: stopAndFail` on both inner steps and the outer pipeline reference, stopping the cluster pipeline. In manual mode, scripts are independent and the operator controls whether to proceed.
