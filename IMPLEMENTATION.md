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

All steps are computed with `__TIMESTAMP__` as a literal placeholder in any path or value that needs run-level isolation (results directories, aggregator paths). Each writer substitutes it differently: the manual writer replaces it with a user-provided `--run-id` value, while the Tekton writer replaces it with a pipeline-runtime expression so that it resolves to the run name at execution time.

```
                                    ┌→ Manual writer  → build/manual/ (__TIMESTAMP__ → run-id)
Step computation → [Step list] ─────┤
                                    └→ Tekton writer  → build/tekton/ (__TIMESTAMP__ → run name at runtime)
```

### Step Computation

Produces a flat list of `Step` dataclasses. Each step is one of two types:

**Generate step** — produces an artifact (Kubernetes manifest or in-pod script):

- `name` — human-readable identity, referenced by command steps via `source`
- `type` — `'generate'`
- `resource_name` — sanitized name for Kubernetes `metadata.name` (pods, services, Tekton tasks); equals `name` when the node name is short and RFC 1123 compliant
- `config.output` — `'manifest'` or `'script'`
- `content` — rendered manifest/script text

**Command step** — represents an action to execute:

- `name` — human-readable identity
- `type` — `'command'`
- `resource_name` — sanitized name for Kubernetes resource references
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
- `finally_step` — marks steps that must run regardless of earlier failures. If `true` and the step has no `test` (global finally — aggregator, cleanup), it runs after all tests complete. If `true` and the step has a `test` (per-test finally-teardown), it is the last step in the test's chain and runs even when earlier steps fail. Writers translate this flag into backend-specific mechanisms.

**Failure policy labelling** — each step carries the test's `on_failure` policy from `test_suite.yaml` (`continue`, `skipTest`, or `abort`). Writers are responsible for translating these labels into backend-specific mechanisms — for example, the Tekton writer uses `onError` values, `when` guards, and guard tasks (see [Failure Policy Handling](#failure-policy-handling)).

The step list is built in three sections — setup, per-test, and teardown:

**Setup steps** (`compute_setup_steps` in `generate.py`):

1. generate `apply-configmap` — ConfigMap manifest with all Go source, cluster.yaml, test_suite.yaml, build.sh, aggregate.py
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

**Node name sanitization:** After loading the cluster config, the generator computes a sanitized version of each node name for use in Kubernetes resource names: invalid characters are replaced with dashes, uppercase is lowercased, and names longer than 16 characters are truncated to 12 characters with a 4-character hash suffix. The sanitized name is stored on `NodeSpec.sanitized_name` and used for pod names, service names, and Tekton task `metadata.name`. The original name is used for `nodeSelector`, labels, label selectors, manual script filenames, and PVC directory paths.

**Name validation:** After all steps are computed, the generator validates pod names for RFC 1123 label compliance and uniqueness — a duplicate would cause resource collisions. Service names are validated for DNS-1035 compliance (must start with a lowercase letter, contain only lowercase alphanumeric characters and hyphens, and end with a lowercase alphanumeric character). The generator aborts with an error if any validation fails.

**Cluster finally steps** (`compute_teardown_steps` in `generate.py`) — teardown steps that run after all tests complete, regardless of success or failure. Each step has `finally_step=True`:

1. generate `create-aggregator` — long-lived Python pod manifest
2. command `create-aggregator` — apply aggregator pod, probe: wait-ready (source: `create-aggregator`)
3. command `aggregate` — exec into aggregator pod to run `aggregate.py`
4. command `cleanup` — delete all pods + services + deployments + configmap

### Manual Writer

`write_manual` in `generate.py` writes steps to `build/manual/`. Manifests go into `manual/manifests/` as data files. Shell scripts go into `manual/` with a `<counter>-` prefix indicating execution order:

1. **Ordering:** Command steps are assigned a counter in execution order, zero-padded to the width of the total step count so that shell glob ordering (`*.sh`) matches execution order. Setup steps get the initial counter values, test steps follow, and teardown steps get the final counter values. Steps that run in parallel across nodes share the same counter.

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

**Pipeline generation:** The Tekton writer produces a single flat cluster pipeline. All tasks — setup, test, and teardown — are entries in one pipeline.

#### Cluster Pipeline

```
apply-configmap → create-builder → build → [test task chains] → finally: create-aggregator → aggregate → cleanup
                                                                          (sequenced via runAfter)
```

Setup and teardown steps are placed directly in the cluster pipeline. For each test in `test_suite.yaml` list order, the cluster pipeline adds task entries based on scope:

- **Node-scoped tests:** one task chain per node, all running in parallel (no `runAfter` between nodes for the same test). Within each chain, tasks are sequential via `runAfter`.
- **Cluster/project-scoped tests:** a single task chain directly in the cluster pipeline. *(not yet implemented)*

Every test, regardless of scope, ends with a guard task. The guard task fans in after all the test's teardown tasks and serves as the single sync point between tests — the next test's first tasks `runAfter` the guard task. The guard task's `onError` is set according to the test's failure policy (see [Failure Policy Handling](#failure-policy-handling)).

Global finally steps (`finally_step=True`, no `test` — aggregator, cleanup) are placed in the cluster pipeline's `finally` block with `onError: continue`, sequenced via `runAfter`.

All tasks reference the pipeline run name directly via `$(context.pipelineRun.name)`.

#### Failure Policy Handling

The Tekton writer translates each step's failure policy label into `onError` values, `when` guards, and guard tasks.

**Guard tasks:** Every test gets a guard task that fans in after all the test's teardown tasks. The guard task receives all non-teardown task statuses as a comma-separated parameter and exits non-zero if any value is `Failed`. The `onError` on the guard task determines the consequence:

- `continue` or `skipTest` → `onError: continue` (pipeline proceeds to the next test regardless)
- `abort` → `onError: stopAndFail` (pipeline halts and jumps to cluster `finally`)

**`onError` assignment:**

| Step category | `onError` |
|---|---|
| Setup steps | `stopAndFail` |
| All test steps | `continue` |
| Per-test finally steps (finally-teardown) | `continue` |
| Global finally steps (aggregator, cleanup) | `continue` |
| Guard tasks (`continue`/`skipTest` policy) | `continue` |
| Guard tasks (`abort` policy) | `stopAndFail` |

**Policy mechanics:**

- **`continue`** — no `when` guards on any steps of the test. Every step runs regardless of failures. Guard task with `onError: continue` — pipeline always proceeds to the next test.

- **`skipTest`** — `when` guards on non-first steps within each node chain, checking `$(tasks.<predecessor>.status) in ["Succeeded"]`. If a step fails, remaining guarded steps on that node are skipped; the per-test teardown (no `when` guard) still runs. Other nodes are unaffected. Guard task with `onError: continue` — pipeline always proceeds to the next test.

- **`abort`** — `when` guards on non-first steps (same as `skipTest`). Other nodes complete the test normally (all remaining steps and teardown). Guard task with `onError: stopAndFail` — if any step failed on any node, the pipeline halts and jumps to cluster `finally`. No further tests run on any node.

When a task is skipped by its `when` guard, its status becomes `None`, causing downstream guarded tasks in the same chain to also skip. The per-test teardown has no `when` guard, so it runs regardless — `scope-when-expressions-to-task` (default since Tekton v0.54) prevents the skip from cascading past unguarded tasks.

#### Test Task Chains

Each test produces one task chain per node (for node-scoped tests) or a single chain (for cluster/project-scoped tests). All tasks in a chain are direct entries in the cluster pipeline, chained via `runAfter`. Each task uses the same name as the step it corresponds to.

```
test-2-inference-wrk-4 chain:
  2-inference-wrk-4-vllm-server
    → 2-inference-wrk-4-pass-fail → 2-inference-wrk-4-cleanup-pass-fail
    → 2-inference-wrk-4-sweep-short-burst → 2-inference-wrk-4-cleanup-sweep-short-burst
    → 2-inference-wrk-4-sweep-sustained-load → 2-inference-wrk-4-cleanup-sweep-sustained-load
    → 2-inference-wrk-4-sweep-long-context → 2-inference-wrk-4-cleanup-sweep-long-context
    → 2-inference-wrk-4-teardown
    → 2-inference-wrk-4-finally-teardown     (no when guard, always runs)
```

#### PipelineRun

- Uses `generateName: uat-cluster-run-` (auto-generated unique name per run)
- Sets `spec.timeouts.pipeline` from `config.yaml`'s `pipelineTimeout` (default `2h`)
- Sets `spec.timeouts.finally` from `config.yaml`'s `finallyTimeout` (default `15m`) — reserves time for aggregation and cleanup so they run even if the pipeline times out
- The generated name becomes the `$(context.pipelineRun.name)` value referenced by all tasks

## Config Field Usage Map

Every parsed config field and where it takes effect. **This is the section to check when adding or auditing fields.**

### TestSuite (`test_suite.yaml`)

| Field | Model | Effect |
|---|---|---|
| `spec.tests[]` | `TestEntry` (list) | Ordered list of tests to run. List order determines execution order across all scopes |
| `spec.tests[].name` | `TestEntry.name` | Test name — resolves to `<name>.yaml` definition and `<name>.go` source |
| `spec.tests[].scope` | `TestEntry.scope` | One of `node`, `cluster`, `project`. Determines execution pattern: node tests fan out to parallel task chains (one per node), cluster tests orchestrate across nodes directly, project tests run without node affinity |
| `spec.tests[].onFailure` | `TestEntry.on_failure` | Per-test failure policy (default: `continue`). `continue`: keep executing remaining steps within this test before proceeding to the next. `skipTest`: skip remaining steps within this test (tear down its resources), proceed to the next test. `abort`: abort the entire suite immediately |
| `spec.tests[].timeout` | `TestEntry.timeout` | Optional per-test timeout for ephemeral test pod completion polling. Overrides `defaultTestTimeout` from `config.yaml`. If omitted, the default is used |

### ClusterTest (`cluster/<name>.yaml`)

| Field | Model | Effect |
|---|---|---|
| `spec.nodes[].name` | `NodeSpec.name` | Node name for `nodeSelector` pinning and step name prefixing. A sanitized version (`NodeSpec.sanitized_name`) is computed at load time for Kubernetes resource names. |
| `spec.nodes[].componentValidation.sanity.gpuCount` | `SanityCheck.gpu_count` | Determines GPU eligibility (> 0). Tests with `requirements.gpu: true` are skipped on nodes with `gpuCount <= 0` |
| `spec.nodes[].componentValidation.*` | `ComponentValidation` (extra="allow") | All fields available in Jinja2 templates as `{{ nodeSpec.componentValidation.* }}` |
| `spec.namespace` | `ClusterTestSpec.namespace` | Kubernetes namespace for all generated resources |
| `spec.storage.pvc` | `StorageConfig.pvc` | PVC name mounted on all pods (via `subPath` — see PVC Directory Hierarchy) |
| `spec.storage.basePath` | `StorageConfig.base_path` | Root of the directory hierarchy on the PVC: `<basePath>/<timestamp>/<step_name>/`. See PVC Directory Hierarchy |

### Test (`<suite-dir>/<test>.yaml`)

| Field | Model | Effect |
|---|---|---|
| `spec.requirements.gpu` | `TestRequirements.gpu` | If `true`, test is skipped on nodes with `gpuCount == 0` |
| `spec.source.ginkgo` | `TestSource` | Path (relative to suite dir) to the Ginkgo test file, read into `LoadedTest`. `go.mod` is generated at build time with the Ginkgo version from `config.yaml` |
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
| `ginkgoVersion` | `ToolConfig.ginkgo_version` | Pinned Ginkgo version for test compilation (default `v2.32.0`). The build script generates `go.mod` with this version and uses `go run` to invoke the matching CLI |
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
  └── Tekton output: write_tekton() replaces '__TIMESTAMP__' → '$(context.pipelineRun.name)'
      All tasks reference $(context.pipelineRun.name) directly.
      Workspace at: /workspace (subPath: <basePath>/$(context.pipelineRun.name)/<step_name>/)
```

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

The `__TIMESTAMP__` placeholder is substituted by each writer: the manual writer replaces it with `--run-id`, the Tekton writer replaces it with a pipeline-runtime expression that resolves to the run name.

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

Pod and service names use the step's `resource_name`, which encodes test_id, test name, and sanitized node name (for node-scoped tests) to avoid collisions. `<node>` below refers to the sanitized node name:

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
│   └── write_steps_file(...)                                       [src/steps_io.py]
│       Serializes steps to steps.json for round-tripping
│
├── write_manual(...)                                               [src/generate.py]
│   Generate steps → write manifests to manifests/ (no counter); command steps → derive numbered shell scripts
│
└── write_tekton(...)                                               [src/generate.py]
    Assigns onError based on step category and failure policy.
    Generates when guards, guard tasks, cluster pipeline, and PipelineRun.
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
| `pipeline.yaml.j2` | Tekton Pipeline (single flat cluster pipeline) |
| `task-guard.yaml.j2` | Guard task (one per test, checks task statuses, `onError` set by failure policy) |
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
| `ToolConfig` | `config.yaml` | `oseCLIImage`, `builderImage`, `ginkgoVersion`, `aggregatorImage`, `configmapName`, `builderPodName`, `aggregatorPodName`, `nodeSelectorKey`, `managedByLabel`, `builderTimeout`, `aggregatorTimeout`, `deployTimeout`, `defaultTestTimeout`, `pipelineTimeout`, `finallyTimeout` |
| `LoadedTest` | (dataclass) | `name`, `spec: TestSpec`, `go_source`, `on_failure`, `timeout`, `test_id`, `scope` |
| `Step` | (dataclass) | `name`, `type` (`generate` or `command`), `config` (type-specific: `output`/`command`/`probe`/`timeout`), `content` (generate only), `source` (command only, list of generate step names), `node` (node name, empty for global steps), `test` (test name, empty for setup/teardown), `test_id` (1-indexed position in test suite, empty for setup/teardown), `on_failure` (test policy: `continue`/`skipTest`/`abort`, empty for setup/teardown), `finally_step` (if `true` and no `test`: placed in cluster pipeline `finally` block; if `true` and has `test`: rendered as regular task with no `when` guard), `scope`, `phase` |
| `StepsFile` | `steps.json` | `metadata` (must contain `toolConfig` and `clusterSpec`), `steps[]` — flat list of serialized steps. Validated on load: step structure, source references, pod name uniqueness, and failure policy labels |

## Resource Requirement Checks and Test Skipping

`node_meets_requirements(requirements, node_spec)` in `node.py` checks each test's requirements against the node spec. If a test requires `gpu: true` but the node has `gpuCount <= 0`, the test is skipped on that node. If ALL tests are skipped for a node, no tasks are generated for that node.

## Known Constraints

- **ConfigMap 1MB limit:** All Go source, cluster config, test suite config, build script, and aggregator script are packed into a single ConfigMap. A project with many tests may exceed Kubernetes' 1MB ConfigMap limit.
- **Resource name length**: step and task names are constructed by concatenating test_id, test name, node, and DAG step (e.g. `2-inference-wrk-4-vllm-server`). These can exceed the 63-character Kubernetes name limit with long test or node names. A future fix is to hash the tuple into a short suffix and carry the full names in labels.
- **One cluster pipeline per namespace**: the builder pod has a fixed name, so only one cluster pipeline can run at a time in a given namespace. This is typically sufficient — the node task chains are the element that scales with cluster size, and a single cluster pipeline fans out to all target nodes in parallel.
- **Sequential sweeps**: parameter sweep entries within a test run as separate pods in sequence. Failure behavior is controlled per-test via the `onFailure` field in `test_suite.yaml` (`continue`, `skipTest`, or `abort`). `continue` uses `onError: continue` with no `when` guards so all tasks run through failures. `skipTest` adds `when` guards that skip remaining tasks in the chain after a failure, but the next test proceeds. `abort` adds a guard task between tests that halts the pipeline if any node had a failure. In manual mode, scripts are independent and the operator controls whether to proceed.
- **Cluster and project test scopes are not yet implemented.** Tests with `scope: cluster` or `scope: project` are loaded and validated but no steps are generated for them.
