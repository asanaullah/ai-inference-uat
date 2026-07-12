<!-- Assisted by Claude Opus 4.6 -->
# UAT Test Harness — Implementation

This document describes how [ARCHITECTURE.md](ARCHITECTURE.md) is implemented. It is intended as a review reference — detailed enough to verify correctness without reading all source files.

## Module Structure

```
src/                     ← Python package (run with python -m src)
  __main__.py            ← entry point, invokes generate.main()
  __init__.py            ← package marker
  generate.py            ← CLI parsing, orchestration, setup/teardown step computation,
  │                         manual writer, Tekton writer, cluster pipeline generation
  common.py              ← Jinja2 engine, manifest validation, file I/O, config loading,
  │                         template context helpers, shared Tekton task generators
  node.py                ← node-level step computation, DAG/test pod rendering,
  │                         node pipeline + task generation, requirement checks
  models.py              ← Pydantic schemas + dataclasses (no internal deps)
  steps_io.py            ← intermediate DAG serialization (write_steps) and
  │                         loading (load_steps) for steps.json round-tripping
scripts/
  aggregate.py           ← JUnit XML aggregation script (deployed via ConfigMap)
templates/
  *.yaml.j2              ← Jinja2 templates for all Kubernetes/Tekton manifests
  *.sh.j2                ← Jinja2 templates for shell scripts
```

**Dependency graph:** `generate.py` → `common.py`, `node.py`, `steps_io.py`. `node.py` → `common.py`. `steps_io.py` → `models.py`. `common.py` → `models.py`. `models.py` has no internal deps.

## Three-Layer Architecture

The generator uses a strict three-layer pipeline. Both output layers consume the same step list — they must never diverge.

All steps are computed with `__TIMESTAMP__` as a literal placeholder in any path or value that needs run-level isolation (results directories, aggregator paths). Each output layer substitutes it differently: the manual writer replaces it with a user-provided `--run-id` value, while the Tekton writer replaces it with `$(params.timestamp)` so that it resolves at pipeline runtime.

```
Layer 1: Step computation    → flat ordered list of Step objects
Layer 2: Manual writer       → build/manual/ (standalone files, __TIMESTAMP__ → run-id)
Layer 3: Tekton writer       → build/tekton/ (self-contained Tekton manifests, __TIMESTAMP__ → $(params.timestamp))
```

### Layer 1: Step Computation

Produces a flat list of `Step` dataclasses. Each step is one of two types:

**Generate step** — produces an artifact (Kubernetes manifest or in-pod script):

- `name` — identity, referenced by command steps via `source`
- `type` — `'generate'`
- `config.output` — `'manifest'` or `'script'`
- `config.onError` — `'stop'` (default: pipeline aborts on failure), `'continue'` (pipeline continues on failure), `'run'` (always runs, placed in Tekton `finally` block)
- `config.timeout` — for probe wait logic
- `content` — rendered manifest/script text

**Command step** — represents an action to execute:

- `name` — identity
- `type` — `'command'`
- `config.command` — `'apply'`, `'delete'`, `'exec'`, etc.
- `config.probe` — `'wait-ready'`, `'poll-completed'`, `'none'`
- `config.onError` — `'stop'` (default: pipeline aborts on failure), `'continue'` (pipeline continues on failure), `'run'` (always runs, placed in Tekton `finally` block)
- `config.timeout` — for probe wait logic
- `source` — list of generate step names whose content to use

The step list is built in three groups — setup, per-node, and teardown:

**Setup steps** (`compute_setup_steps` in `generate.py`):

1. generate `configmap` — ConfigMap manifest with all Go source, go.mod, go.sum, cluster.yaml, test_suite.yaml, build.sh, aggregate.py
2. command `apply-configmap` — apply configmap (source: `configmap`)
3. generate `builder-pod` — long-lived Go toolchain pod manifest
4. command `create-builder` — apply builder pod, probe: wait-ready (source: `builder-pod`)
5. command `build` — exec into builder pod to run `build.sh`

**Node steps** (`compute_node_steps` in `node.py`): per-node, per-test, numbered sequentially:

1. For each persistent DAG step: generate `<node>-<step>` manifest (pod + optional service, joined with `---`) + command `deploy-<step>` (apply, probe: wait-ready)
2. For each non-persistent DAG step: generate manifest + command (apply, probe: poll-completed) — one per sweep entry (or one if no sweep)
3. If test had persistent steps: command `teardown-<test>` (delete by label, onError: stop) + command `finally-teardown-<test>` (delete by label, onError: run)

**Teardown steps** (`compute_teardown_steps` in `generate.py`):

1. generate `aggregator-pod` — long-lived Python pod manifest
2. command `create-aggregator` — apply aggregator pod, probe: wait-ready (source: `aggregator-pod`)
3. command `aggregate` — exec into aggregator pod to run `aggregate.py` (onError: run)
4. command `cleanup` — delete all pods + configmap (onError: run)

### Layer 2: Manual Writer

`write_manual` in `generate.py` writes steps to `build/manual/{setup,nodes/<node>,teardown}/`:

- **Generate steps:** content is written as-is. Manifests are plain text, scripts get chmod 755.
- **Command steps:** the writer derives a shell script from the command config (e.g. `apply` → `oc apply -f <source>`, `delete` → `oc delete -l <selector>`, `exec` → `oc exec <pod> -- <args>`).

After writing, `__TIMESTAMP__` is replaced with the `--run-id` value in all output.

### Layer 3: Tekton Writer

`write_tekton` in `generate.py` derives Tekton Tasks and Pipelines from the same step list. Generate steps provide the manifest/script content embedded in tasks. Command steps determine the Tekton task type based on `config.command` + `config.probe`:

| Command + Probe | Tekton task behavior | Template |
|---|---|---|
| `apply` + `none` | Apply manifest | `task-apply-wait-ready.yaml.j2` |
| `apply` + `wait-ready` | Apply manifest, poll until Ready | `task-apply-wait-ready.yaml.j2` |
| `apply` + `poll-completed` | Apply manifest, poll until Succeeded/Failed | `task-run-test-pod.yaml.j2` |
| `exec` | Exec into target pod, run command | `task-build.yaml.j2` |
| `delete` (by label) | Delete resources matching selector | `task-teardown.yaml.j2` |
| `delete` (all pods) | Delete all pods + configmap | `task-cleanup.yaml.j2` |

Steps with `onError: 'run'` are placed in the Tekton `finally` block. Steps with `onError: 'continue'` get `onError: continue` on the Tekton task. Steps with `onError: 'stop'` use default Tekton behavior.

## Config Field Usage Map

Every parsed config field and where it takes effect. **This is the section to check when adding or auditing fields.**

### TestSuite (`test_suite.yaml`)

| Field | Model | Effect |
|---|---|---|
| `spec.tests.node` | `TestCategories.node` | Ordered list of node-scoped test names to generate |
| `spec.tests.cluster` | `TestCategories.cluster` | Loaded but not yet implemented (printed if non-empty) |
| `spec.tests.project` | `TestCategories.project` | Loaded but not yet implemented (printed if non-empty) |
| `spec.execution.stopOnFailure` | `ExecutionConfig.stop_on_failure` | When `false` (default): test command steps get `onError: 'continue'` so the pipeline doesn't abort on test failure. When `true`: test command steps use default `onError: 'stop'` (pipeline stops on first task failure). Only affects Tekton output; manual scripts are independent. |

### ClusterTest (`cluster/<name>.yaml`)

| Field | Model | Effect |
|---|---|---|
| `spec.nodes[].name` | `NodeSpec.name` | Node name for `nodeSelector` pinning and `<node>-<step>` prefixed resource names |
| `spec.nodes[].componentValidation.sanity.gpuCount` | `SanityCheck.gpu_count` | Determines GPU eligibility (> 0). Tests with `requirements.gpu: true` are skipped on nodes with 0 GPUs |
| `spec.nodes[].componentValidation.*` | `ComponentValidation` (extra="allow") | All fields available in Jinja2 templates as `{{ nodeSpec.componentValidation.* }}` |
| `spec.namespace` | `ClusterTestSpec.namespace` | Kubernetes namespace for all generated resources |
| `spec.storage.pvc` | `StorageConfig.pvc` | PVC name mounted on all pods (via `subPath` — see PVC Directory Hierarchy) |
| `spec.storage.basePath` | `StorageConfig.base_path` | Root of the directory hierarchy on the PVC: `<basePath>/<timestamp>/node/<node>/<test>/<dag>/`. See PVC Directory Hierarchy |

### Test (`<suite-dir>/<test>.yaml`)

| Field | Model | Effect |
|---|---|---|
| `spec.requirements.gpu` | `TestRequirements.gpu` | If `true`, test is skipped on nodes with `gpuCount <= 0` |
| `spec.source.{ginkgo,goMod,goSum}` | `TestSource` | Paths (relative to suite dir) to Go source files, read into `LoadedTest` |
| `spec.dag[].persistsThroughSweep` | `DAGStep.persists_through_sweep` | `true`: rendered as generate + command (apply, wait-ready) pod (+ service); stays up for all sweep entries. `false`: rendered as generate + command (apply, poll-completed) pod; one per sweep entry |
| `spec.dag[].service` | `DAGStep.service` | If `enabled: true`, generates a Service manifest and populates `{{ services["name"].url }}` in template context |
| `spec.dag[].command` | `DAGStep.command` | Structured command: `args` + `flags` → `["arg1", "--key=value"]`. Persistent steps have `serverConfig` variables substituted into command args. Non-persistent steps render through template context |
| `spec.dag[].labelFilter` | `DAGStep.label_filter` | If set and no `command`: generates a ginkgo command with `--ginkgo.label-filter=<value>`. Also auto-injects `RESULTS_DIR` env var if not already present |
| `spec.dag[].parameterSweep` | `DAGStep.parameter_sweep` | If set: one test pod per `entries[]`. Each entry's `flags` are merged over `baseCommand.flags`. If null: single test pod using the step's own command |
| `spec.dag[].env` | `DAGStep.env` | Env vars. Values are rendered through Jinja2 with the full template context |
| `spec.dag[].resources` | `DAGStep.resources` | Resource requests/limits. Values are rendered with the `nodeSpec` context, so Jinja2 expressions (e.g. `{{ nodeSpec.componentValidation.sanity.gpuCount }}`) work in both persistent and non-persistent steps. |
| `spec.dag[].volumeMounts` | `DAGStep.volume_mounts` | Extra volume mounts added to the container. Must pair with `volumes` entries |
| `spec.dag[].volumes` | `DAGStep.volumes` | Raw volume definitions (list of dicts). Rendered as-is via `to_yaml` filter. For test pods, these are in addition to the hardcoded PVC volume |
| `spec.dag[].ports` | `DAGStep.ports` | Container ports (persistent DAG steps only) |
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
| `testTimeout` | `ToolConfig.test_timeout` | Timeout for test pod completion polling (default `600s`) |
| `pipelineTimeout` | `ToolConfig.pipeline_timeout` | Sets `spec.timeouts.pipeline` on the PipelineRun manifest (default `2h`) |
| `finallyTimeout` | `ToolConfig.finally_timeout` | Sets `spec.timeouts.finally` on the PipelineRun manifest — reserves time for aggregation and cleanup after pipeline timeout (default `15m`) |

## Timestamp Flow (Critical Path)

The timestamp is used for results path isolation between runs. Getting it wrong means the aggregator can't find results.

```
main() computes all steps with timestamp='__TIMESTAMP__'
  │
  ├── Manual output: _stamp() replaces '__TIMESTAMP__' → args.run_id (e.g. 'manual-run')
  │   Workspace at: /workspace (subPath: <basePath>/<run-id>/node/<node>/<test>/<dag>/)
  │
  └── Tekton output: task-run-test-pod.yaml.j2 replaces '__TIMESTAMP__' → '$(params.timestamp)'
      │
      ├── Cluster pipeline:
      │   - $(context.pipelineRun.name) = top-level PipelineRun name
      │   - Passes it as 'timestamp' param to each node pipeline via pipelineRef
      │   - Passes it as 'timestamp' param to aggregate-results task
      │
      └── Node pipeline:
          - Declares 'timestamp' as a pipeline parameter
          - Test tasks reference $(params.timestamp) — the value passed from the cluster pipeline
          - Workspace at: /workspace (subPath: <basePath>/$(params.timestamp)/node/<node>/<test>/<dag>/)
```

**Invariant:** The node pipeline's `$(params.timestamp)` and the aggregator's `$(params.timestamp)` must resolve to the same value. Both receive `$(context.pipelineRun.name)` from the cluster pipeline. The node pipeline must NOT use `$(context.pipelineRun.name)` directly — that would resolve to the child PipelineRun name in Pipeline-in-Pipeline, which differs from the parent.

## PVC Directory Hierarchy and Volume Mounting

Every DAG step gets a unique directory on the PVC, computed transparently by the generator from the step's position in the hierarchy. Test authors do not specify paths — they write to `/workspace` and files land in the right place.

### Directory Hierarchy

```
<PVC root>/
  <basePath>/
    <timestamp>/
      binaries/
        <test_name>/
          test.bin
      node/
        <node_name>/
          <test_name>/
            <dag_step_name>/
              ... (junit.xml, logs, benchmark output, etc.)
      cluster/                     (future — not yet implemented)
        <test_name>/
          <dag_step_name>/
      project/                     (future — not yet implemented)
        <test_name>/
          <dag_step_name>/
      report/
        summary.json
```

Concrete example with `basePath=uat/results`:

```
uat/results/uat-cluster-run-abc12/
  binaries/
    component/test.bin
    inference/test.bin
  node/
    wrk-4/
      component/
        test-runner/
          junit.xml
      inference/
        vllm-server/              ← persistent DAG pod workspace (logs, cache)
        pass-fail/
          junit.xml
        short-burst/
          junit.xml
          results.json
        sustained-load/
          junit.xml
        long-context/
          junit.xml
    wrk-6/
      ...
  report/
    summary.json
```

### Path Computation

The generator computes workspace paths deterministically from the step's position in the hierarchy.

| Scope | Path formula |
|---|---|
| Node | `<basePath>/__TIMESTAMP__/node/<node>/<test_name>/<dag_step_name>` |
| Cluster (future) | `<basePath>/__TIMESTAMP__/cluster/<test_name>/<dag_step_name>` |
| Project (future) | `<basePath>/__TIMESTAMP__/project/<test_name>/<dag_step_name>` |

The `__TIMESTAMP__` placeholder is substituted by each output layer: the manual writer replaces it with `--run-id`, the Tekton writer replaces it with `$(params.timestamp)`.

### Pod Volume Mounting

Each pod type mounts the PVC with a `subPath` scoped to its role. DAG pods also get a second mount at `/binaries` for access to compiled test binaries.

| Pod type | `/workspace` | `/binaries` | Notes |
|---|---|---|---|
| Builder | subPath: `<basePath>/<ts>/binaries/` | — | Writes to `/workspace/<test>/test.bin` |
| Aggregator | subPath: `<basePath>/<ts>/` | — | Scans `node/`, `cluster/`, `project/` recursively for `junit.xml` |
| Persistent DAG pod | subPath: `<basePath>/<ts>/node/<node>/<test>/<dag>/` | subPath: `<basePath>/<ts>/binaries/` | Server logs, model cache written to `/workspace` |
| Ephemeral test pod | subPath: `<basePath>/<ts>/node/<node>/<test>/<dag>/` | subPath: `<basePath>/<ts>/binaries/` | `junit.xml` written to `/workspace` |

Because `/workspace` IS the DAG step's unique directory:
- Test pods write `junit.xml` to `/workspace/junit.xml`
- Ginkgo binaries are accessed at `/binaries/<test>/test.bin`
- Benchmark tools use `output-dir: /workspace`

## Pod Name Conventions

All pod and service names are prefixed with the node name to avoid collisions in the shared namespace:

| Resource | Name pattern | Example |
|---|---|---|
| Persistent DAG pod | `<node>-<dag_step.name>` | `wrk-4-vllm-server` |
| Service | `<node>-<service.name>` | `wrk-4-vllm-server` |
| Test pod | `<node>-test-<test_name>-<suffix>` | `wrk-4-test-inference-short-burst` |
| Builder pod | `<tc.builder_pod_name>` (predefined name) | `ginkgo-builder` |
| Aggregator pod | `<tc.aggregator_pod_name>` (predefined name) | `uat-aggregator` |

Service URLs in the template context use the prefixed name: `{{ services["vllm-server"].url }}` → `http://wrk-4-vllm-server:8000`.

## Tekton Pipeline Structure

### Cluster Pipeline (`uat-cluster`)

```
create-builder → build → [run-wrk-4, run-wrk-6, ...] → finally: create-aggregator → aggregate → cleanup
                                   (parallel, pipelineRef)                         (sequenced via runAfter)
```

- Node pipelines are referenced via `pipelineRef` (Tekton Pipeline-in-Pipeline)
- Each node pipeline receives `timestamp` param = `$(context.pipelineRun.name)` from the cluster pipeline
- `aggregate-results` and `cleanup` are in the `finally` block (run regardless of success/failure)
- `cleanup` has `runAfter: [aggregate-results]` to ensure aggregation completes first

### Node Pipeline (`uat-node-<node>`)

```
spec.params: [timestamp: string]

tasks (sequential via runAfter):
  run-test-component-test-runner
    → deploy-vllm-server
    → run-test-inference-pass-fail
    → run-test-inference-short-burst
    → run-test-inference-sustained-load
    → run-test-inference-long-context
    → teardown-inference

finally:
  finally-teardown-inference   ← ensures GPU resources are freed even on failure
```

- All tasks are chained sequentially via `runAfter`
- Test command steps receive `timestamp` via `$(params.timestamp)` (pipeline param, not context)
- When `stopOnFailure: false`: test command steps have `onError: continue`
- Teardown command steps with `onError: run` are placed in the `finally` block (one per test)

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
| `to_yaml` | `yaml.dump(block_style)` | Inline structured data (env, ports, resources) |
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
| `paramSweep.id` | Sweep entry `id` or DAG step `name` | `short-burst` |
| `paramSweep.command` | Resolved sweep command list | Used with `\| toJson` |
| `nodeSpec.*` | Full node spec from cluster config | `{{ nodeSpec.componentValidation.sanity.gpuCount }}` |
| `services["name"]` | Service context from persistent DAG steps | `{{ services["vllm-server"].url }}` |
| `timestamp` | `__TIMESTAMP__` placeholder | Replaced at output time |
| `node` | Node name | `wrk-4` |


## Call Graph

```
__main__.py → main()                                               [src/generate.py]

main()
├── load_tool_config(config_path)                                   [src/common.py]
├── load_config(suite_dir, cluster_path)                            [src/common.py]
│
├── compute_setup_steps(...)                                        [src/generate.py]
│   Produces generate + command steps for configmap, builder pod, build
│
├── compute_node_steps(...)                                         [src/node.py]
│   Per node, per test: produces generate + command steps for
│   DAG deployment, test execution, and teardown
│
├── compute_teardown_steps(...)                                     [src/generate.py]
│   Produces generate + command steps for aggregator pod, aggregation, cleanup
│
├── write_manual(...)                                               [src/generate.py]
│   Generate steps → write files; command steps → derive shell scripts
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
| `dag-service.yaml.j2` | Kubernetes Service for persistent DAG pods |
| `test-pod.yaml.j2` | Run-to-completion test pods with PVC mount |
| `build.sh.j2` | Shell script to compile all Ginkgo binaries |

**Tekton task templates** (used by the Tekton writer for command steps):

| Template | Produces |
|---|---|
| `pipeline.yaml.j2` | Tekton Pipeline (shared by cluster and node) |
| `pipelinerun.yaml.j2` | Tekton PipelineRun with timeout |
| `task-apply-wait-ready.yaml.j2` | Tekton Task: apply manifest + wait Ready |
| `task-build.yaml.j2` | Tekton Task: exec build.sh in builder pod |
| `task-run-test-pod.yaml.j2` | Tekton Task: apply test pod + poll Succeeded/Failed |
| `task-teardown.yaml.j2` | Tekton Task: label-based delete |
| `task-cleanup.yaml.j2` | Tekton Task: delete all pods + configmap |

**Manual script templates** (may be replaced by command step derivation in the manual writer):

| Template | Produces |
|---|---|
| `exec-script.sh.j2` | `oc exec` wrapper scripts |
| `teardown-script.sh.j2` | Label-based `oc delete` for test resources |
| `cleanup-script.sh.j2` | Final cleanup of all pods + configmap |

## Pydantic Model Reference

| Model | YAML source | Key fields |
|---|---|---|
| `TestSuite` | `test_suite.yaml` | `spec.tests.{node,cluster,project}`, `spec.execution.stopOnFailure` |
| `Test` | `<test>.yaml` | `spec.dag[]`, `spec.source`, `spec.serverConfig`, `spec.requirements` |
| `DAGStep` | nested in `Test` | `name`, `type`, `image`, `command`, `env`, `service`, `persistsThroughSweep`, `parameterSweep`, `labelFilter`, `resources`, `volumeMounts`, `volumes`, `privileged` |
| `ParameterSweep` | nested in `DAGStep` | `baseCommand.{args,flags}`, `entries[].{id,description,flags}` |
| `ClusterTest` | `cluster/*.yaml` | `spec.nodes[]`, `spec.namespace`, `spec.storage.{pvc,basePath}` |
| `NodeSpec` | nested in `ClusterTest` | `name`, `componentValidation.sanity.gpuCount` (typed), all others via `extra="allow"` |
| `ToolConfig` | `config.yaml` | `oseCLIImage`, `builderImage`, `aggregatorImage`, `configmapName`, `builderPodName`, `aggregatorPodName`, `nodeSelectorKey`, `managedByLabel`, `builderTimeout`, `aggregatorTimeout`, `deployTimeout`, `testTimeout`, `pipelineTimeout`, `finallyTimeout` |
| `LoadedTest` | (dataclass) | `spec: TestSpec`, `go_source`, `go_mod`, `go_sum` |
| `Step` | (dataclass) | `name`, `type` (`generate` or `command`), `config` (type-specific: `output`/`command`/`probe`/`onError`/`timeout`), `content` (generate only), `source` (command only, references generate step name) |

## Resource Requirement Checks and Test Skipping

`node_meets_requirements(requirements, node_spec)` in `node.py` checks each test's requirements against the node spec. If a test requires `gpu: true` but the node has `gpuCount <= 0`, the test is skipped on that node. If ALL tests are skipped for a node, no node pipeline is generated.

## Known Constraints

- **ConfigMap 1MB limit:** All Go source + go.mod/go.sum is packed into a single ConfigMap. A project with many tests or large go.sum files may exceed Kubernetes' 1MB ConfigMap limit.
- **Cluster and project test scopes are not yet implemented.** The `tests.cluster` and `tests.project` lists are loaded and validated but no steps are generated for them.
