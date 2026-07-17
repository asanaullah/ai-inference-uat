<!-- Assisted by Claude Opus 4.6 -->
# UAT Test Harness — Implementation

This document describes how [ARCHITECTURE.md](ARCHITECTURE.md) is implemented. It is intended as a review reference — detailed enough to verify correctness without reading all source files.

## Module Structure

```
src/                     ← Python package (run with python -m src)
  __main__.py            ← entry point, invokes generate.main()
  __init__.py            ← package marker
  generate.py            ← CLI parsing, orchestration, setup/teardown step computation,
  │                         manual writer, Tekton writer
  common.py              ← Jinja2 engine, manifest validation, file I/O, config loading,
  │                         template context helpers, command building
  node.py                ← node-level step computation, DAG/test pod rendering,
  │                         requirement checks
  cluster.py             ← cluster-level step computation (placeholder)
  project.py             ← project-level step computation (placeholder)
  models.py              ← Pydantic schemas + dataclasses (no internal deps)
  steps_io.py            ← intermediate DAG serialization (write_steps_file) and
  │                         loading (load_steps_file) for steps.json round-tripping
scripts/
  aggregate.py           ← JUnit XML aggregation script (deployed via ConfigMap)
templates/
  *.yaml.j2              ← Jinja2 templates for all Kubernetes/Tekton manifests
  *.sh.j2                ← Jinja2 templates for shell scripts
```

**Dependency graph:** `generate.py` → `common.py`, `models.py`, `node.py`, `cluster.py`, `project.py`, `steps_io.py`. `node.py` → `common.py`, `models.py`. `cluster.py`, `project.py` → `models.py`. `steps_io.py` → `models.py`. `common.py` → `models.py`. `models.py` has no internal deps.

## Compute / Write Architecture

The generator separates **what to run** (step computation) from **how to run it** (writers). Step computation produces a single ordered list of steps — the complete specification of every resource and action needed for the test suite. Writers are independent consumers that each translate the same step list into a different execution format. Adding a new execution backend (e.g. Argo Workflows, GitHub Actions) means writing a new writer — step computation doesn't change.

All steps are computed with `__TIMESTAMP__` as a literal placeholder in any path or value that needs run-level isolation (results directories, aggregator paths). Each writer substitutes it differently: the manual writer replaces it with a user-provided `--run-id` value, while the Tekton writer replaces it with `$(params.timestamp)` so that it resolves at pipeline runtime.

```
                                    ┌→ Manual writer  → build/manual/ (__TIMESTAMP__ → run-id)
Step computation → [Step list] ─────┤
                                    └→ Tekton writer  → build/tekton/ (__TIMESTAMP__ → $(params.timestamp))
```

### Step Computation

Produces a flat list of `Step` dataclasses. Each step is one of two types:

**Generate step** — produces an artifact (Kubernetes manifest or in-pod script):

- `name` — identity, referenced by command steps via `source`
- `type` — `'generate'`
- `config.output` — `'manifest'` or `'script'`
- `content` — rendered manifest/script text

**Command step** — represents an action to execute:

- `name` — identity
- `type` — `'command'`
- `config.command` — `'apply'`, `'exec'`, `'delete'`, `'delete-all'`
- `config.probe` — `'wait-ready'`, `'poll-completed'`, `'none'`
- `config.timeout` — for probe wait logic
- `config.pod_name` — target pod for apply+wait-ready / apply+poll-completed steps (also used for uniqueness validation)
- `config.target` — pod to exec into (exec steps only)
- `config.args` — command arguments (exec steps only)
- `config.selector` — label selector for delete steps (e.g. `test=inference,node=wrk-4,sweep=pass-fail`)
- `config.configmap_name` — ConfigMap name for delete-all steps
- `config.managed_by_label` — managed-by label value for delete-all steps
- `config.service_name` — service name for generate steps with an associated Service (used for DNS-1035 validation)
- `source` — list of generate step names whose content to use

**Step-level fields** (set during computation, used by writers):

- `phase` — `'setup'`, `'test'`, or `'teardown'`. Both writers group steps by phase to separate setup, per-test, and teardown output.
- `scope` — `'node'`, `'cluster'`, or `'project'` (empty for setup/teardown). Determines execution pattern in the manual writer and pipeline structure in the Tekton writer.
- `finally_step` — if `true`, the Tekton writer places this step in the pipeline's `finally` block.

**`onError` assignment** — `config.onError` is not set during step computation. After all steps are computed, `generate.py` walks the step list and assigns `onError` to command steps by looking up the test's `onFailure` policy via `step.test`:

- Setup steps (`finally_step=False`, no `test`): `onError: stopAndFail` (abort pipeline on setup failure)
- Global finally steps (`finally_step=True`, no `test` — aggregator, cleanup): `onError: continue` (keep going through teardown failures)
- Test steps (`finally_step=False`, has `test`): `onError: continue` if the test's `onFailure == "continue"`, otherwise `onError: stopAndFail`
- Per-test finally steps (`finally_step=True`, has `test` — finally-teardown): no `onError` needed (Tekton `finally` always runs regardless of errors)

The step list is built in three sections — setup, per-test, and teardown:

**Setup steps** (`compute_setup_steps` in `generate.py`):

1. generate `apply-configmap` — ConfigMap manifest with all Go source, go.mod, go.sum, cluster.yaml, test_suite.yaml, build.sh, aggregate.py
2. command `apply-configmap` — apply configmap (source: `apply-configmap`)
3. generate `create-builder` — long-lived Go toolchain pod manifest
4. command `create-builder` — apply builder pod, probe: wait-ready (source: `create-builder`)
5. command `build` — exec into builder pod to run `build.sh`

Binaries are compiled once per test name and stored at `binaries/<test_name>/test.bin`, not per `test_id`. If the same test appears multiple times in `test_suite.yaml`, all instances share the same `<test>.go` source file — they differ only in runtime config (`onFailure`, `timeout`, sweep parameters), not in compiled code.

**Test steps**: per-test, with scope determining the execution pattern. Each scope has its own step computation function. All step names follow the unified naming convention: `<test_id>-<test>-<dag_step.name>` for cluster/project scope, `<test_id>-<test>-<node>-<dag_step.name>` for node scope, with `-<id>` appended for sweep entries. `<test_id>` is the 1-indexed position of the test in the `test_suite.yaml` list (not zero-padded). The same test can appear multiple times in the list (e.g. with different configs or failure policies), so `<test_id>` prevents collisions in resource names and results paths, while `<test_name>` provides readability. The common pattern across scopes:

1. For each persistent DAG step: generate manifest (pod + optional service) + command to deploy and wait for readiness
2. For each non-persistent DAG step (one per sweep entry, or one if no sweep): generate manifest + command to run and poll for completion + command to delete pods, services, and deployments by sweep label. The `sweep` label value is the sweep entry's `id` for sweep steps, or the DAG step's `name` for non-sweep steps
3. If test had persistent steps: command to tear down persistent resources
4. command `<test_id>-<test>[-<node>]-finally-teardown` (delete by label, `finally_step=True`) — always generated for every test

**Node scope** (`compute_node_steps` in `node.py`): steps are generated per-node, per-test. Labels include `node=<node>` for targeted cleanup.

1. For each persistent DAG step: generate `<test_id>-<test>-<node>-<dag_step>` manifest (pod + optional service, joined with `---`) + command `<test_id>-<test>-<node>-<dag_step>` (apply, probe: wait-ready). Service names are prefixed with `svc-` for DNS-1035 compliance.
2. For each non-persistent DAG step: generate manifest + command (apply, probe: poll-completed) + command `<test_id>-<test>-<node>-cleanup-<dag_step>[-<id>]` (delete by label)
3. If test had persistent steps: command `<test_id>-<test>-<node>-teardown` (delete by label)
4. command `<test_id>-<test>-<node>-finally-teardown` (delete by label, `finally_step=True`)

**Cluster and project scopes** are not yet implemented (`cluster.py` and `project.py` are placeholder modules that return empty step lists, and the Tekton writer rejects any non-node-scoped test steps with a `ValueError`). Step names follow the same convention without the `<node>` segment (e.g. `<test_id>-<test>-<dag_step>`). Cluster tests will orchestrate across nodes directly (e.g. server on node A, client on node B). Project tests will run without node affinity.

**Name validation:** After all steps are computed, the generator validates pod names for RFC 1123 label compliance (lowercase alphanumeric, hyphens, etc.) and uniqueness (since they are used as pod names, PVC directories, filenames, and Tekton task names — a duplicate would cause resource collisions), and service names for DNS-1035 compliance (must start with a lowercase letter, contain only lowercase alphanumeric characters and hyphens, and end with a lowercase alphanumeric character). The generator aborts with an error if any validation fails.

**Cluster finally steps** (`compute_teardown_steps` in `generate.py`) — placed in the cluster pipeline's `finally` block. Each step gets `onError: continue` so that cleanup runs even if aggregation fails:

1. generate `create-aggregator` — long-lived Python pod manifest
2. command `create-aggregator` — apply aggregator pod, probe: wait-ready (source: `create-aggregator`)
3. command `aggregate` — exec into aggregator pod to run `aggregate.py`
4. command `cleanup` — delete all pods + services + deployments + configmap

### Manual Writer

`write_manual` in `generate.py` writes steps to `build/manual/`. Manifests go into `manual/manifests/` as data files. Shell scripts go into `manual/` with a `<counter>-` prefix indicating execution order:

1. **Ordering:** Command steps are assigned a counter in execution order. Setup steps get the initial counter values, test steps follow, and teardown steps get the final counter values. Steps that run in parallel across nodes share the same counter.

2. **Writing:** For each step:
   - **Generate steps (manifests):** content is written as `manifests/<name>.yaml` — no counter prefix. These are reference data, not actions.
   - **Command steps (apply):** a shell script is generated that applies the manifest and handles the probe. For `probe: none`: just `oc apply`. For `probe: wait-ready`: apply, then `oc wait --for=condition=Ready` with the step's timeout, then tail recent logs. For `probe: poll-completed`: apply, wait for the pod to start, stream logs in real time with `oc logs -f`, then poll for a terminal phase before checking the result (exits non-zero on failure). Written as `<counter>-<name>.sh`.
   - **Command steps (exec, delete, delete-all):** a shell script is derived from the step config. Written as `<counter>-<name>.sh`. Per-test `finally-teardown` steps are included — they give the operator a single "clean up everything for this test" script, useful when a step fails mid-test.
   - All manual scripts include echo statements so the operator can follow progress without reading the script source.
   - Step names already encode the test_id, test name, and node, so no additional prefixing is needed.

3. **Timestamp substitution:** `__TIMESTAMP__` is replaced with the `--run-id` value in all output.

### Tekton Writer

`write_tekton` in `generate.py` derives Tekton Tasks and Pipelines from the same step list. Generate steps provide the manifest/script content embedded in tasks. Command steps determine the Tekton task type based on `config.command` + `config.probe`:

| Command + Probe | Tekton task behavior | Template |
|---|---|---|
| `apply` + `none` | Apply manifest | `task-apply-wait-ready.yaml.j2` |
| `apply` + `wait-ready` | Apply manifest, poll until Ready | `task-apply-wait-ready.yaml.j2` |
| `apply` + `poll-completed` | Apply manifest, poll until Succeeded/Failed | `task-run-test-pod.yaml.j2` |
| `exec` | Exec into target pod, run command | `task-exec.yaml.j2` |
| `delete` | Delete pods, services, and deployments matching selector | `task-teardown.yaml.j2` |
| `delete-all` | Delete all pods + services + deployments + configmap | `task-cleanup.yaml.j2` |

**Pipeline generation:** The Tekton writer groups each node's steps by test ID. For each test on each node, it generates:

1. A **test pipeline** (`test-<test_id>-<test>-<node>`) containing:
   - **tasks:** all non-finally steps for that test, chained with `runAfter`. Each task uses the same name as the step it corresponds to.
   - **finally:** steps with `finally_step=True` (the `finally-teardown` step)

2. A **node pipeline** (`node-<test_id>-<test>-<node>`) that wraps the test pipeline with a single `pipelineRef`. There is one node pipeline per (node × node-scoped test) combination — not one per node.

The inner test pipeline's step-level `onError` values (already assigned by `generate.py` post-processing) determine whether execution continues within the test on step failure.

**Cluster pipeline:** Setup and teardown steps remain directly in the cluster pipeline. For each test in `test_suite.yaml` list order, the cluster pipeline adds entries based on scope:

- **Node-scoped tests:** one `pipelineRef` per node pointing to `node-<test_id>-<test>-<node>`, all running in parallel. The next test's entries use `runAfter` on all node pipelines of the previous test.
- **Cluster/project-scoped tests:** a single `pipelineRef` to the test pipeline (`test-<test_id>-<test>`) directly. *(not yet implemented)*

The `onError` on each pipeline reference is determined by the test's `on_failure` policy:

- `continue` or `skipTest` → `onError: continue` (cluster pipeline proceeds to the next test)
- `abort` → `onError: stopAndFail` (a failure on any single node is enough to stop the cluster pipeline)

## Config Field Usage Map

Every parsed config field and where it takes effect. **This is the section to check when adding or auditing fields.**

### TestSuite (`test_suite.yaml`)

| Field | Model | Effect |
|---|---|---|
| `spec.tests[]` | `TestEntry` (list) | Ordered list of tests to run. List order determines execution order across all scopes |
| `spec.tests[].name` | `TestEntry.name` | Test name — resolves to `<name>.yaml` definition and `<name>.go` source |
| `spec.tests[].scope` | `TestEntry.scope` | One of `node`, `cluster`, `project`. Determines execution pattern: node tests fan out to parallel node pipelines, cluster tests orchestrate across nodes directly, project tests run without node affinity |
| `spec.tests[].onFailure` | `TestEntry.on_failure` | Per-test failure policy (default: `continue`). `continue`: keep executing remaining steps within this test before proceeding to the next. `skipTest`: skip remaining steps within this test (tear down its resources), proceed to the next test. `abort`: abort the entire suite immediately |
| `spec.tests[].timeout` | `TestEntry.timeout` | Optional per-test timeout for ephemeral test pod completion polling. Overrides `defaultTestTimeout` from `config.yaml`. If omitted, the default is used |

### ClusterTest (`cluster/<name>.yaml`)

| Field | Model | Effect |
|---|---|---|
| `spec.nodes[].name` | `NodeSpec.name` | Node name for `nodeSelector` pinning and step name prefixing (e.g. `<test_id>-<test>-<node>-<dag_step>`) |
| `spec.nodes[].componentValidation.sanity.gpuCount` | `SanityCheck.gpu_count` | Determines GPU eligibility (> 0). Tests with `requirements.gpu: true` are skipped on nodes with `gpuCount <= 0` |
| `spec.nodes[].componentValidation.*` | `ComponentValidation` (extra="allow") | All fields available in Jinja2 templates as `{{ nodeSpec.componentValidation.* }}` |
| `spec.namespace` | `ClusterTestSpec.namespace` | Kubernetes namespace for all generated resources |
| `spec.storage.pvc` | `StorageConfig.pvc` | PVC name mounted on all pods (via `subPath` — see PVC Directory Hierarchy) |
| `spec.storage.basePath` | `StorageConfig.base_path` | Root of the directory hierarchy on the PVC: `<basePath>/<timestamp>/<step_name>/`. See PVC Directory Hierarchy |

### Test (`<suite-dir>/<test>.yaml`)

| Field | Model | Effect |
|---|---|---|
| `spec.requirements.gpu` | `TestRequirements.gpu` | If `true`, test is skipped on nodes with `gpuCount == 0` |
| `spec.source.{ginkgo,goMod,goSum}` | `TestSource` | Paths (relative to suite dir) to Go source files, read into `LoadedTest` |
| `spec.dag[].persistsThroughSweep` | `DAGStep.persists_through_sweep` | `true`: rendered as generate + command (apply, wait-ready) pod (+ service); stays up for all sweep entries. `false`: rendered as generate + command (apply, poll-completed) pod; one per sweep entry |
| `spec.dag[].service` | `DAGStep.service` | If `enabled: true`, generates a Service manifest and populates `{{ services["name"].url }}` in template context. `headless: true` (default) creates a headless Service (ClusterIP: None) |
| `spec.dag[].command` | `DAGStep.command` | Structured command: `args` + `flags` → `["arg1", "--key=value"]`. Both persistent and non-persistent steps render command args through the Jinja2 template context (`serverConfig`, `nodeSpec`, `services`, `node`, `timestamp`). Non-persistent steps additionally have `paramSweep` available |
| `spec.dag[].labelFilter` | `DAGStep.label_filter` | If set, takes priority over `command`: generates a ginkgo command with `--ginkgo.label-filter=<value>` and `--ginkgo.junit-report=/workspace/junit.xml`. Also auto-injects `RESULTS_DIR` env var if not already present |
| `spec.dag[].parameterSweep` | `DAGStep.parameter_sweep` | If set: one test pod per `entries[]`. Each entry's `flags` are merged over `baseCommand.flags`. If null: single test pod using the step's own command |
| `spec.dag[].env` | `DAGStep.env` | Env vars. Values are rendered through Jinja2 with the full template context |
| `spec.dag[].resources` | `DAGStep.resources` | Resource requests/limits. Values are rendered through Jinja2 with the full template context (`nodeSpec`, `serverConfig`, `services`, `node`, `timestamp`), so expressions like `{{ nodeSpec.componentValidation.sanity.gpuCount }}` work in both persistent and non-persistent steps. |
| `spec.dag[].volumeMounts` | `DAGStep.volume_mounts` | Extra volume mounts added to the container. Must pair with `volumes` entries |
| `spec.dag[].volumes` | `DAGStep.volumes` | Raw volume definitions (list of dicts). Rendered as-is via `to_yaml` filter. For test pods, these are in addition to the hardcoded PVC volume |
| `spec.dag[].ports` | `DAGStep.ports` | Container ports |
| `spec.dag[].readinessProbe` | `DAGStep.readiness_probe` | Readiness probe (persistent DAG steps only) |
| `spec.dag[].privileged` | `DAGStep.privileged` | If `true`: sets `securityContext.privileged: true` and `hostPID: true` |
| `spec.serverConfig` | `TestSpec.server_config` | Dict of variables available in Jinja2 templates as `{{ serverConfig.* }}` |

### ToolConfig (`config.yaml`)

| Field | Model | Effect |
|---|---|---|
| `oseCLIImage` | `ToolConfig.ose_cli_image` | Image for Tekton task steps (runs `oc` commands) |
| `builderImage` | `ToolConfig.builder_image` | Image for the Go builder pod |
| `aggregatorImage` | `ToolConfig.aggregator_image` | Image for the Python aggregator pod |
| `configmapName` | `ToolConfig.configmap_name` | Fixed name for the source-delivery ConfigMap |
| `builderPodName` | `ToolConfig.builder_pod_name` | Fixed name for the builder pod |
| `aggregatorPodName` | `ToolConfig.aggregator_pod_name` | Fixed name for the aggregator pod |
| `nodeSelectorKey` | `ToolConfig.node_selector_key` | Kubernetes label key for nodeSelector (e.g. `kubernetes.io/hostname`) |
| `managedByLabel` | `ToolConfig.managed_by_label` | Value for `app.kubernetes.io/managed-by` label |
| `builderTimeout` | `ToolConfig.builder_timeout` | Timeout for builder pod readiness probe (default `300s`) |
| `aggregatorTimeout` | `ToolConfig.aggregator_timeout` | Timeout for aggregator pod readiness probe (default `120s`) |
| `deployTimeout` | `ToolConfig.deploy_timeout` | Timeout for DAG pod readiness probes (default `600s`) |
| `defaultTestTimeout` | `ToolConfig.default_test_timeout` | Default timeout for test pod completion polling (default `600s`). Can be overridden per-test via `timeout` in `test_suite.yaml` |
| `pipelineTimeout` | `ToolConfig.pipeline_timeout` | Sets `spec.timeouts.pipeline` on the PipelineRun manifest (default `2h`) |
| `finallyTimeout` | `ToolConfig.finally_timeout` | Sets `spec.timeouts.finally` on the PipelineRun manifest — reserves time for aggregation and cleanup after pipeline timeout (default `15m`) |

## Timestamp Flow (Critical Path)

The timestamp is used for results path isolation between runs. Getting it wrong means the aggregator can't find results.

```
main() computes all steps with timestamp='__TIMESTAMP__'
  │
  ├── Manual output: _stamp() replaces '__TIMESTAMP__' → args.run_id (e.g. 'manual-run')
  │   Workspace at: /workspace (subPath: <basePath>/<run-id>/<step_name>/)
  │
  └── Tekton output: write_tekton() replaces '__TIMESTAMP__' → '$(params.timestamp)' in Python
      │
      ├── Cluster pipeline:
      │   - $(context.pipelineRun.name) = top-level PipelineRun name
      │   - Passes it as 'timestamp' param to every task and pipeline reference:
      │     setup tasks, node pipelines, and finally-block tasks (aggregator, aggregate, cleanup)
      │
      └── Node pipeline (one per node × test):
          - Declares 'timestamp' as a pipeline parameter
          - Passes it to the test pipeline via pipelineRef
          - Test tasks reference $(params.timestamp) — the value passed from the cluster pipeline
          - Workspace at: /workspace (subPath: <basePath>/$(params.timestamp)/<step_name>/)
```

**Invariant:** The node pipeline's `$(params.timestamp)` and the aggregator's `$(params.timestamp)` must resolve to the same value. Both receive `$(context.pipelineRun.name)` from the cluster pipeline. The node pipeline must NOT use `$(context.pipelineRun.name)` directly — that would resolve to the child PipelineRun name in Pipeline-in-Pipeline, which differs from the parent.

## PVC Directory Hierarchy and Volume Mounting

Every DAG step gets a unique directory on the PVC, named after the step. The step name encodes all hierarchy information (test_id, test name, node, DAG step), so directories are flat under the timestamp. Test authors do not specify paths — they write to `/workspace` and files land in the right place.

### Directory Hierarchy

Each step's workspace directory is named after its step name. All step directories are flat siblings under `<basePath>/<timestamp>/`:

```
<PVC root>/
  <basePath>/
    <timestamp>/
      binaries/
        <test_name>/
          test.bin
      <step_name>/                   ← one flat directory per step
        ... (junit.xml, logs, benchmark output, etc.)
      report/
        summary.json
```

Concrete example with `basePath=uat/results`, two node-scoped tests (component, inference) followed by a cluster-scoped test:

```
uat/results/uat-cluster-run-abc12/
  binaries/
    component/test.bin
    inference/test.bin
  1-component-wrk-4-test-runner/
    junit.xml
  1-component-wrk-6-test-runner/
    junit.xml
  2-inference-wrk-4-vllm-server/           ← persistent DAG pod workspace (logs, cache)
  2-inference-wrk-4-pass-fail/
    junit.xml
  2-inference-wrk-4-sweep-short-burst/
    junit.xml
    results.json
  2-inference-wrk-4-sweep-sustained-load/
    junit.xml
  2-inference-wrk-4-sweep-long-context/
    junit.xml
  2-inference-wrk-6-vllm-server/
  2-inference-wrk-6-pass-fail/
    junit.xml
  ...
  3-network-client/                        ← cluster-scoped (no node segment, planned)
    junit.xml
  report/
    summary.json
```

### Path Computation

The generator computes workspace paths deterministically from the step name.

| Scope | Path formula |
|---|---|
| Node | `<basePath>/__TIMESTAMP__/<test_id>-<test>-<node>-<dag_step>` |
| Node (with sweep) | `<basePath>/__TIMESTAMP__/<test_id>-<test>-<node>-<dag_step>-<id>` |
| Cluster (future) | `<basePath>/__TIMESTAMP__/<test_id>-<test>-<dag_step>` |
| Cluster (future, with sweep) | `<basePath>/__TIMESTAMP__/<test_id>-<test>-<dag_step>-<id>` |
| Project (future) | `<basePath>/__TIMESTAMP__/<test_id>-<test>-<dag_step>` |
| Project (future, with sweep) | `<basePath>/__TIMESTAMP__/<test_id>-<test>-<dag_step>-<id>` |

The `__TIMESTAMP__` placeholder is substituted by each writer: the manual writer replaces it with `--run-id`, the Tekton writer replaces it with `$(params.timestamp)`.

### Pod Volume Mounting

Each pod type mounts the PVC with a `subPath` scoped to its role. DAG pods also get a second mount at `/binaries` for access to compiled test binaries.

| Pod type | `/workspace` | `/binaries` | Notes |
|---|---|---|---|
| Builder | subPath: `<basePath>/<ts>/binaries` | — | Writes to `/workspace/<test>/test.bin` |
| Aggregator | subPath: `<basePath>/<ts>` | — | Scans step directories for `junit.xml` |
| Persistent DAG pod (node) | subPath: `<basePath>/<ts>/<step_name>` | subPath: `<basePath>/<ts>/binaries` | Server logs, model cache written to `/workspace` |
| Ephemeral test pod (node) | subPath: `<basePath>/<ts>/<step_name>` | subPath: `<basePath>/<ts>/binaries` | `junit.xml` written to `/workspace` |

Because `/workspace` IS the step's unique directory:
- Test pods write `junit.xml` to `/workspace/junit.xml`
- Ginkgo binaries are accessed at `/binaries/<test>/test.bin`
- Benchmark tools use `output-dir: /workspace`

## Pod Name Conventions

Pod and service names use the step name, which encodes test_id, test name, and node (for node-scoped tests) to avoid collisions:

| Resource | Name pattern | Example |
|---|---|---|
| Persistent DAG pod (node) | `<test_id>-<test>-<node>-<dag_step.name>` | `2-inference-wrk-4-vllm-server` |
| Service (node) | `svc-<test_id>-<test>-<node>-<service.name>` | `svc-2-inference-wrk-4-vllm-server` |
| Test pod (node) | `<test_id>-<test>-<node>-<dag_step.name>` | `1-component-wrk-4-test-runner` |
| Test pod (node, sweep) | `<test_id>-<test>-<node>-<dag_step.name>-<id>` | `2-inference-wrk-4-sweep-short-burst` |
| Persistent DAG pod (cluster/project) | `<test_id>-<test>-<dag_step.name>` | `3-network-client` |
| Test pod (cluster/project) | `<test_id>-<test>-<dag_step.name>` | `3-network-client` |
| Test pod (cluster/project, sweep) | `<test_id>-<test>-<dag_step.name>-<id>` | `3-network-client-short-burst` |
| Builder pod | `<tc.builder_pod_name>` (predefined name) | `ginkgo-builder` |
| Aggregator pod | `<tc.aggregator_pod_name>` (predefined name) | `uat-aggregator` |

Service names are prefixed with `svc-` for DNS-1035 compliance (services must start with a letter, while pod names follow RFC 1123 which allows leading digits). Service URLs in the template context use the full service name: `{{ services["vllm-server"].url }}` → `http://svc-2-inference-wrk-4-vllm-server:8000`.

## Tekton Pipeline Structure

### Cluster Pipeline (`uat-cluster`)

```
apply-configmap → create-builder → build → [tests in list order] → finally: create-aggregator → aggregate → cleanup
                                                                                                (sequenced via runAfter)
```

- Tests execute in `test_suite.yaml` list order; each test is a separate entry in the cluster pipeline
- Node-scoped tests produce one `pipelineRef` per node (to `node-<test_id>-<test>-<node>`), all running in parallel. The next test's entries wait on all node pipelines of the previous test via `runAfter`
- Cluster-scoped tests produce a single `pipelineRef` to the test pipeline directly *(not yet implemented)*
- Project-scoped tests run without node affinity *(not yet implemented)*
- Each node pipeline receives `timestamp` param = `$(context.pipelineRun.name)` from the cluster pipeline
- Per-test `onFailure` determines `onError` on each pipeline reference: `continue` or `skipTest` → `onError: continue`, `abort` → `onError: stopAndFail` (a failure on any single node stops the cluster pipeline)
- `create-aggregator`, `aggregate`, and `cleanup` are in the `finally` block (run regardless of success/failure), each with `onError: continue` so cleanup runs even if aggregation fails
- `aggregate` has `runAfter: [create-aggregator]`, `cleanup` has `runAfter: [aggregate]` — sequenced within the finally block

### Node Pipeline (`node-<test_id>-<test>-<node>`)

Each node pipeline runs exactly one test on one node. It contains a single `pipelineRef` to the test pipeline. Pods within the test are pinned to the target node via `nodeSelector` in the pod manifests.

```
spec.params: [timestamp: string]

tasks:
  test-1-component-wrk-4 (pipelineRef → test-1-component-wrk-4)
```

### Test Pipeline (`test-<test_id>-<test>-<node>`)

```
spec.params: [timestamp: string]

tasks (chained via runAfter):
  2-inference-wrk-4-vllm-server
    → 2-inference-wrk-4-pass-fail → 2-inference-wrk-4-cleanup-pass-fail
    → 2-inference-wrk-4-sweep-short-burst → 2-inference-wrk-4-cleanup-sweep-short-burst
    → 2-inference-wrk-4-sweep-sustained-load → 2-inference-wrk-4-cleanup-sweep-sustained-load
    → 2-inference-wrk-4-sweep-long-context → 2-inference-wrk-4-cleanup-sweep-long-context
    → 2-inference-wrk-4-teardown
finally: 2-inference-wrk-4-finally-teardown
```

- Test command steps receive `timestamp` via `$(params.timestamp)` (pipeline param, not context)
- Per-test `onFailure` determines step-level `onError` within the test pipeline: `continue` → `onError: continue` on each step, `skipTest` or `abort` → `onError: stopAndFail`
- The `finally` block contains `<test_id>-<test>-<node>-finally-teardown` — hardcoded to always run, cleaning up all the test's resources (both persistent and ephemeral) regardless of success or failure

### PipelineRun

- Uses `generateName: uat-cluster-run-` (auto-generated unique name per run)
- Sets `spec.timeouts.pipeline` from `config.yaml`'s `pipelineTimeout` (default `2h`)
- Sets `spec.timeouts.finally` from `config.yaml`'s `finallyTimeout` (default `15m`) — reserves time for aggregation and cleanup so they run even if the pipeline times out
- The generated name becomes the `$(context.pipelineRun.name)` value that flows through the timestamp chain

## Jinja2 Template Engine

Configured in `common.py` with:
- `StrictUndefined` — missing variables raise errors (catches typos in templates)
- `trim_blocks` + `lstrip_blocks` — clean YAML output from `{% if %}` blocks
- `keep_trailing_newline` — files end with newline

### Custom Filters

| Filter | Implementation | Used for |
|---|---|---|
| `to_yaml` | `yaml.dump(default_flow_style=False)` | Inline structured data (env, ports, resources) |
| `toJson` | `json.dumps` | Serializing sweep commands as JSON in env vars |
| `yaml_quote` | Custom quoting logic | Safe YAML value embedding |
| `shell_join` | `shlex.join` | Joining command args for shell execution |

### Manifest Validation

`render_manifest()` in `common.py` validates all `.yaml.j2` output:
- Parses with `yaml.safe_load_all` (handles multi-document)
- Checks each document has `apiVersion`, `kind`, `metadata.name` (or `generateName`)
- Aborts the generator on failure — broken manifests are never written to disk

Non-YAML templates (`.sh.j2`) skip manifest validation. Jinja2's `StrictUndefined` still catches missing template variables, and as the manual writer moves toward deriving scripts from command step config, freeform shell templates become less common. If scripts grow more complex, `bash -n` (syntax check without execution) could be added as a validation step.

## Template Context Variables

Available in test YAML Jinja2 expressions (`command`, `env` values):

| Variable | Source | Example |
|---|---|---|
| `serverConfig.*` | `spec.serverConfig` from test YAML | `{{ serverConfig.model }}` |
| `paramSweep.id` | Sweep entry `id` or DAG step `name` (ephemeral steps only) | `short-burst` |
| `paramSweep.command` | Resolved sweep command list (ephemeral steps only, only present for sweep entries) | Used with `\| toJson` |
| `nodeSpec.*` | Full node spec from cluster config | `{{ nodeSpec.componentValidation.sanity.gpuCount }}` |
| `services["name"]` | Service context from DAG steps with `service.enabled` | `{{ services["vllm-server"].url }}` |
| `timestamp` | `__TIMESTAMP__` placeholder | Replaced at output time |
| `node` | Node name | `wrk-4` |


## Call Graph

```
__main__.py → main()                                               [src/generate.py]

main()
├── if --steps:
│   ├── load_steps_file(path)                                       [src/steps_io.py]
│   │   Loads steps from a previously written steps.json (skips computation)
│   ├── _validate_unique_pod_names(steps)                           [src/generate.py]
│   └── _validate_service_names(steps)                              [src/generate.py]
│
├── else:
│   ├── load_tool_config(config_path)                               [src/common.py]
│   ├── load_config(suite_dir, cluster_path)                        [src/common.py]
│   │
│   ├── compute_setup_steps(...)                                    [src/generate.py]
│   │   Produces generate + command steps for configmap, builder pod, build
│   │
│   ├── compute_node_steps(...)                                     [src/node.py]
│   │   Per node, per test: produces generate + command steps for
│   │   DAG deployment, test execution, and teardown
│   │
│   ├── compute_cluster_steps(...)                                  [src/cluster.py]
│   │   Cluster-scoped test steps (placeholder, returns empty list)
│   │
│   ├── compute_project_steps(...)                                  [src/project.py]
│   │   Project-scoped test steps (placeholder, returns empty list)
│   │
│   ├── compute_teardown_steps(...)                                 [src/generate.py]
│   │   Produces generate + command steps for aggregator pod, aggregation, cleanup
│   │
│   ├── _validate_unique_pod_names(steps)                           [src/generate.py]
│   │   Validates pod names for RFC 1123 compliance and uniqueness
│   │
│   ├── _validate_service_names(steps)                              [src/generate.py]
│   │   Validates service names for DNS-1035 compliance
│   │
│   ├── assign_on_error(steps)                                      [src/generate.py]
│   │   Post-processing: walks all steps and assigns config.onError
│   │   based on step category (setup/teardown/test/finally)
│   │
│   └── write_steps_file(...)                                       [src/steps_io.py]
│       Serializes steps (with onError already assigned) to steps.json
│       for round-tripping
│
├── write_manual(...)                                               [src/generate.py]
│   Generate steps → write manifests to manifests/ (no counter); command steps → derive numbered shell scripts
│
└── write_tekton(...)                                               [src/generate.py]
    Routes command steps to Tekton task generators by command + probe config.
    Generates node pipelines, cluster pipeline, and PipelineRun.
```

## Template File Reference

**Generate step templates** (produce content for generate steps):

| Template | Produces |
|---|---|
| `configmap.yaml.j2` | ConfigMap with all source files |
| `support-pod.yaml.j2` | Builder and aggregator pods (sleep infinity) |
| `dag-pod.yaml.j2` | Persistent DAG pods with PVC mount (e.g. vLLM server) |
| `dag-service.yaml.j2` | Kubernetes Service for DAG pods (persistent or ephemeral) |
| `test-pod.yaml.j2` | Run-to-completion test pods with PVC mount |

| `build.sh.j2` | Shell script to compile all Ginkgo binaries (rendered during setup, embedded in ConfigMap) |

**Tekton task templates** (used by the Tekton writer for command steps):

| Template | Produces |
|---|---|
| `pipeline.yaml.j2` | Tekton Pipeline (shared by all pipelines) |
| `pipelinerun.yaml.j2` | Tekton PipelineRun with timeout |
| `task-apply-wait-ready.yaml.j2` | Tekton Task: apply manifest + wait Ready |
| `task-exec.yaml.j2` | Tekton Task: exec command in target pod |
| `task-run-test-pod.yaml.j2` | Tekton Task: apply test pod + poll Succeeded/Failed |
| `task-teardown.yaml.j2` | Tekton Task: label-based delete |
| `task-cleanup.yaml.j2` | Tekton Task: delete all pods + services + deployments + configmap |

**Manual script templates** (derived from command step config by the manual writer):

| Template | Produces |
|---|---|
| `apply-script.sh.j2` | `oc apply -f` scripts referencing a manifest file |
| `exec-script.sh.j2` | `oc exec` wrapper scripts |
| `teardown-script.sh.j2` | Label-based `oc delete` for test resources |
| `cleanup-script.sh.j2` | Final cleanup of all pods + services + deployments + configmap |

## Pydantic Model Reference

| Model | YAML source | Key fields |
|---|---|---|
| `TestSuite` | `test_suite.yaml` | `spec.tests[]` — ordered list of `TestEntry` (name, scope, onFailure, timeout) |
| `Test` | `<test>.yaml` | `spec.dag[]`, `spec.source`, `spec.serverConfig`, `spec.requirements` |
| `DAGStep` | nested in `Test` | `name`, `image`, `command`, `env`, `service`, `ports`, `readinessProbe`, `resources`, `volumeMounts`, `volumes`, `privileged`, `persistsThroughSweep`, `parameterSweep`, `labelFilter` |
| `ParameterSweep` | nested in `DAGStep` | `baseCommand.{args,flags}`, `entries[].{id,description,flags}` |
| `ClusterTest` | `cluster/*.yaml` | `spec.nodes[]`, `spec.namespace`, `spec.storage.{pvc,basePath}` |
| `NodeSpec` | nested in `ClusterTest` | `name`, `componentValidation.sanity.gpuCount` (typed), all others via `extra="allow"` |
| `ToolConfig` | `config.yaml` | `oseCLIImage`, `builderImage`, `aggregatorImage`, `configmapName`, `builderPodName`, `aggregatorPodName`, `nodeSelectorKey`, `managedByLabel`, `builderTimeout`, `aggregatorTimeout`, `deployTimeout`, `defaultTestTimeout`, `pipelineTimeout`, `finallyTimeout` |
| `LoadedTest` | (dataclass) | `name`, `spec: TestSpec`, `go_source`, `go_mod`, `go_sum`, `on_failure`, `timeout`, `test_id`, `scope` |
| `Step` | (dataclass) | `name`, `type` (`generate` or `command`), `config` (type-specific: `output`/`command`/`probe`/`timeout`; `onError` added by post-processing), `content` (generate only), `source` (command only, list of generate step names), `node` (node name, empty for global steps), `test` (test name, empty for setup/teardown), `test_id` (1-indexed position in test suite, empty for setup/teardown), `on_failure` (test policy: `continue`/`skipTest`/`abort`, empty for setup/teardown), `finally_step` (if `true`, placed in Tekton `finally` block), `scope`, `phase` |
| `StepsFile` | `steps.json` | `metadata` (must contain `toolConfig` and `clusterSpec`), `steps[]` — flat list of serialized steps. Validated on load: step structure, source references, pod name uniqueness, and onError correctness |

## Resource Requirement Checks and Test Skipping

`node_meets_requirements(requirements, node_spec)` in `node.py` checks each test's requirements against the node spec. If a test requires `gpu: true` but the node has `gpuCount <= 0`, the test is skipped on that node. If ALL tests are skipped for a node, no node pipeline is generated.

## Known Constraints

- **ConfigMap 1MB limit:** All Go source, go.mod/go.sum, cluster config, test suite config, build script, and aggregator script are packed into a single ConfigMap. A project with many tests or large go.sum files may exceed Kubernetes' 1MB ConfigMap limit.
- **Resource name length**: step and pipeline names are constructed by concatenating test_id, test name, node, and DAG step (e.g. `2-inference-wrk-4-vllm-server`, `node-2-inference-wrk-4`). These can exceed the 63-character Kubernetes name limit with long test or node names. A future fix is to hash the tuple into a short suffix and carry the full names in labels.
- **One cluster pipeline per namespace**: the builder pod has a fixed name, so only one cluster pipeline can run at a time in a given namespace. This is typically sufficient — the node pipelines are the element that scales with cluster size, and a single cluster pipeline fans out to all target nodes in parallel.
- **Sequential sweeps**: parameter sweep entries within a test run as separate pods in sequence. Failure behavior is controlled per-test via the `onFailure` field in `test_suite.yaml` (`continue`, `skipTest`, or `abort`). Each test is its own pipeline: `continue` sets `onError: continue` on inner steps so the test runs through failures; `skipTest` sets `onError: stopAndFail` on inner steps so the test stops on first failure but the cluster pipeline proceeds to the next test; `abort` sets `onError: stopAndFail` on both inner steps and the outer pipeline reference, stopping the cluster pipeline. In manual mode, scripts are independent and the operator controls whether to proceed.
- **Cluster and project test scopes are not yet implemented.** Tests with `scope: cluster` or `scope: project` are loaded and validated but no steps are generated for them.
