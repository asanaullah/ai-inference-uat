<!-- Assisted by Claude Opus 4.6 -->
# UAT Test Harness — Implementation

This document describes how [ARCHITECTURE.md](ARCHITECTURE.md) is implemented. It is intended as a review reference — detailed enough to verify correctness without reading all source files.

## Module Structure

```
src/                     ← Python package (run with python -m src)
  __main__.py            ← entry point, invokes main.main()
  __init__.py            ← package marker
  main.py                ← CLI parsing, orchestration (config loading, step computation
  │                         dispatch, validation, writer invocation)
  step_generator.py      ← setup/teardown step computation, pod/service name validation,
  │                         step list serialization (write_steps_file) and loading
  │                         (load_steps_file) for steps.json round-tripping
  common.py              ← Jinja2 engine, manifest validation, config loading,
  │                         template context helpers, command building,
  │                         persistent/ephemeral/teardown step rendering
  node.py                ← node-level step computation, DAG/test pod rendering,
  │                         requirement checks
  cluster.py             ← cluster-level step computation, placement resolution
  │                         (set generation, node filtering, nodeSelector assignment)
  project.py             ← project-level step computation (single chain, no node affinity)
  models.py              ← Pydantic schemas + dataclasses (no internal deps)
  writers/
    manual.py            ← manual writer (numbered shell scripts + YAML manifests)
    tekton.py            ← Tekton writer (Tasks, Pipeline, PipelineRun)
scripts/
  aggregate.py           ← JUnit XML aggregation script (deployed via ConfigMap)
templates/
  *.yaml.j2              ← Jinja2 templates for all Kubernetes/Tekton manifests
  *.sh.j2                ← Jinja2 templates for shell scripts
```

**Dependency graph:** `main.py` → `common.py`, `models.py`, `node.py`, `cluster.py`, `project.py`, `step_generator.py`, `writers/manual.py`, `writers/tekton.py`. `step_generator.py` → `common.py`, `models.py`. `writers/manual.py` → `common.py`, `models.py`. `writers/tekton.py` → `common.py`, `models.py`. `node.py` → `common.py`, `models.py`. `cluster.py` → `common.py`, `models.py`. `project.py` → `common.py`, `models.py`. `common.py` → `models.py`. `models.py` has no internal deps.

## Compute / Write Architecture

The generator is invoked as `python -m src` with three required inputs: `--test-suite` (path to `test_suite.yaml`), `--test-lib` (directory containing `<test>.yaml` and `<test>.go` files), and `--cluster` (path to the cluster config). An alternative `--steps` flag accepts a previously written `steps.json`, skipping step computation and re-running only the writers — useful for editing steps externally or regenerating output with different options. The manual writer also accepts `--run-id` to set the timestamp substitution value.

`main()` in `main.py` branches on `--steps`: if provided, it loads the step list from `steps.json` via `load_steps_file()`, re-validates pod and service names, and proceeds directly to the writers. Otherwise, it loads the three input files, computes steps, validates, serializes to `steps.json`, and then runs both writers.

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
- `lifecycle` — `true` for automatically generated lifecycle steps (per-ephemeral cleanup, teardown, and finally-teardown). These steps receive no `when` guards under any failure policy, and their statuses are excluded from the guard task's pass/fail check. Writers use this flag to distinguish test steps (which may be guarded) from lifecycle steps (which always run).

**Failure policy labelling** — each step carries the test's `on_failure` policy from `test_suite.yaml` (`continue`, `skipTest`, or `abort`). Writers are responsible for translating these labels into backend-specific mechanisms — for example, the Tekton writer uses `onError` values, `when` guards, and guard tasks (see [Failure Policy Handling](#failure-policy-handling)).

The step list is built in three sections — setup, per-test, and teardown:

**Setup steps** (`compute_setup_steps` in `step_generator.py`):

1. generate `apply-configmap` — ConfigMap manifest with all Go source, cluster.yaml, test_suite.yaml, build.sh, aggregate.py
2. command `apply-configmap` — apply configmap (source: `apply-configmap`)
3. generate `create-builder` — long-lived Go toolchain pod manifest
4. command `create-builder` — apply builder pod, probe: wait-ready (source: `create-builder`)
5. command `build` — exec into builder pod to run `build.sh`

Binaries are compiled once per test name and stored at `binaries/<test_name>/test.bin`, not per `test_id`. If the same test appears multiple times in `test_suite.yaml`, all instances share the same `<test>.go` source file — they differ only in runtime config (`onFailure`, `timeout`, sweep parameters), not in compiled code.

**Spec override resolution:** Before step computation, `load_config()` in `common.py` loads each test definition from `<test>.yaml` in the test library. If a `TestEntry` in `test_suite.yaml` includes a `spec` section, it is deep-merged over the loaded test definition's `spec`. The merge is recursive for dict fields (`serverConfig`) — nested keys are merged, not replaced. For `dag` overrides, the suite entry uses DAG step names as dict keys (not a list): each key is matched to a DAG step by `name`, and only the specified fields within that step are overridden — unmentioned fields and unmentioned DAG steps retain their `<test>.yaml` defaults. After merging, the result is re-validated by constructing a new `TestSpec` from the merged dict — this catches invalid types, unknown fields, or malformed DAG step definitions introduced by the suite-level override. Without re-validation, such errors would only surface during step computation or manifest rendering with less clear error messages. The validated `TestSpec` becomes the `LoadedTest.spec` used for all subsequent step computation. This allows the same test definition in the test library to produce different runtime configurations across suite entries without duplicating the test file.

**Test steps**: per-test, with scope determining the execution pattern. Each scope has its own step computation function. All step names follow the unified naming convention: `<test_id>-<test>-<node>-<dag_step.name>` for node scope, `<test_id>-<test>-set<i>-<dag_step.name>` for cluster scope with multiple sets, `<test_id>-<test>-<dag_step.name>` for cluster scope with a single set or project scope, with `-<id>` appended for sweep entries. `<test_id>` is the 1-indexed position of the test in the `test_suite.yaml` list (not zero-padded). The same test can appear multiple times in the list (e.g. with different configs or failure policies), so `<test_id>` prevents collisions in resource names and results paths, while `<test_name>` provides readability. The common pattern across scopes:

1. For each persistent DAG step: generate manifest (pod + optional service) + command to deploy and wait for readiness. Service names are prefixed with `svc-` for DNS-1035 compliance; service URL references in env vars and commands are rewritten to match
2. For each non-persistent DAG step (one per sweep entry, or one if no sweep): generate manifest + command to run and poll for completion + command to delete pods, services, and deployments by sweep label. The `sweep` label value is the sweep entry's `id` for sweep steps, or the DAG step's `name` for non-sweep steps
3. If test had persistent steps: command to tear down persistent resources
4. command `<test_id>-<test>[-<node>|-set<i>]-finally-teardown` (delete by label, `finally_step=True`) — always generated for every test

**Node scope** (`compute_node_steps` in `node.py`): steps are generated per-node, per-test. Labels include `node=<node>` for targeted cleanup.

1. For each persistent DAG step: generate `<test_id>-<test>-<node>-<dag_step>` manifest (pod + optional service, joined with `---`) + command `<test_id>-<test>-<node>-<dag_step>` (apply, probe: wait-ready)
2. For each non-persistent DAG step: generate manifest + command (apply, probe: poll-completed) + command `<test_id>-<test>-<node>-cleanup-<dag_step>[-<id>]` (delete by label)
3. If test had persistent steps: command `<test_id>-<test>-<node>-teardown` (delete by label)
4. command `<test_id>-<test>-<node>-finally-teardown` (delete by label, `finally_step=True`)

**Cluster scope** (`compute_cluster_steps` in `cluster.py`): placement is fully resolved during step computation. The function proceeds in three phases:

**Phase 1 — Node filtering:** If the suite entry's `placement.setRequirements` is non-empty, each node from the cluster config is checked against the requirements. Numeric fields in `componentValidation.sanity` are treated as minimums (node value must be ≥ required), string fields as exact matches. Nodes that fail any requirement are excluded. If no nodes pass, no steps are generated.

**Phase 2 — Set generation:** From the filtered node list, sets of size `placement.setSize` are generated. `setType: permutation` generates ordered tuples (using `itertools.permutations`), where (A,B) and (B,A) are distinct sets. `setType: combination` generates unordered groups (using `itertools.combinations`), where {A,B} = {B,A}. `setSelection: random` picks a single random set from the generated list. `setSelection: all` uses all generated sets, clamped by `setCutoff` if non-zero (`min(setCutoff, len(sets))`).

**Phase 3 — Step generation:** For each set, steps are generated following the same lifecycle as a node-scoped chain. The number of chains is always resolved algorithmically from placement config — Phase 2 determines how many sets survive filtering, selection, and cutoff, and each surviving set becomes exactly one chain. Sets are ordered sequentially in the step list — each set's steps follow the previous set's `finally-teardown`. Multi-set runs include a `set<i>` segment in step names; single-set runs omit it. Multi-set pods and services carry a `chain` label with the set key (e.g., `chain=set0`), and cleanup selectors include this label to scope teardown to the current set — preventing leakage between sets if a teardown fails. Single-set runs omit the `chain` label since `test=<name>` is sufficient with only one chain.

The `setSize` determines how nodeSelectors are assigned within each chain:

- **`setSize == 1`**: all DAG steps in the chain share the same node (the single node in the set). The chain structure is identical to a node-scoped chain — the only difference is naming (no `<node>` segment, optional `set<i>` segment) and that the node was selected by placement config rather than fan-out.

- **`setSize > 1`**: the number of DAG steps must equal `setSize` — the generator validates this and aborts if they don't match. DAG step *i* gets a `nodeSelector` pinning it to node *i* of the set. This means different steps within the same chain run on different nodes (e.g., a server on node A, a client on node B). The lifecycle is the same (persistent deploy, ephemeral run, per-ephemeral cleanup, teardown, finally-teardown), but resources are distributed across the set's nodes rather than colocated.

1. For each persistent DAG step: generate `<test_id>-<test>-[set<i>-]<dag_step>` manifest (pod + optional service) + command (apply, probe: wait-ready)
2. For each non-persistent DAG step: generate manifest + command (apply, probe: poll-completed) + command `<test_id>-<test>-[set<i>-]cleanup-<dag_step>[-<id>]` (delete by label)
3. If test had persistent steps: command `<test_id>-<test>-[set<i>-]teardown` (delete by label)
4. command `<test_id>-<test>-[set<i>-]finally-teardown` (delete by label, `finally_step=True`)

The `setMappings` metadata (recording which nodes are in each set, keyed by `test_id`) is written to `steps.json` by `write_steps_file()` — useful for `random` selection where the chosen set is non-deterministic.

**Project scope** (`compute_project_steps` in `project.py`): produces a single chain without node affinity. No placement resolution or node filtering — the function takes the test definition and generates steps directly, without iterating over nodes or sets. Pods are rendered without `nodeSelector`, so the Kubernetes scheduler places them freely. Step names follow the convention `<test_id>-<test>-<dag_step>`. Labels include only the test-level identifiers (no node or set labels), so cleanup targets all resources for the test. The step generation pattern is identical to a single node-scoped chain, minus the node segment in names and the `nodeSelector` in manifests.

1. For each persistent DAG step: generate `<test_id>-<test>-<dag_step>` manifest (pod + optional service) + command (apply, probe: wait-ready)
2. For each non-persistent DAG step: generate manifest + command (apply, probe: poll-completed) + command `<test_id>-<test>-cleanup-<dag_step>[-<id>]` (delete by label)
3. If test had persistent steps: command `<test_id>-<test>-teardown` (delete by label)
4. command `<test_id>-<test>-finally-teardown` (delete by label, `finally_step=True`)

**Node name sanitization:** After loading the cluster config, the generator computes a sanitized version of each node name for use in Kubernetes resource names: invalid characters are replaced with dashes, uppercase is lowercased, and names longer than 16 characters are truncated to 12 characters with a 4-character hash suffix. The sanitized name is stored on `NodeSpec.sanitized_name` and used for pod names, service names, and Tekton task `metadata.name`. The original name is used for `nodeSelector`, labels, label selectors, manual script filenames, and PVC directory paths.

**Name validation:** After all steps are computed, the generator validates pod names for RFC 1123 label compliance and uniqueness — a duplicate would cause resource collisions. Service names are validated for DNS-1035 compliance (must start with a lowercase letter, contain only lowercase alphanumeric characters and hyphens, and end with a lowercase alphanumeric character). The generator aborts with an error if any validation fails.

**Resource validation:** Before step computation for each node-scoped or cluster-scoped test, the generator validates that each target node has sufficient resources for the test's peak concurrent demand. `validate_node_resources(test, node_spec, jinja_env)` in `common.py` performs the check:

1. Builds a minimal Jinja2 render context: `{"nodeSpec": node_spec_dict, "serverConfig": test.spec.server_config}`.
2. For each DAG step with `resources.requests`, renders each value through Jinja2 to resolve template expressions (e.g., `{{ nodeSpec.componentValidation.sanity["nvidia.com/gpu"] }}` → `4`).
3. Classifies each DAG step as persistent (`persistsThroughSweep: true`) or ephemeral (default). Each ephemeral DAG step contributes one entry to the ephemeral demand list (sweep entries share the same resource requests, so they produce the same demand).
4. Aggregates per resource type: `peak_demand = sum(persistent) + max(ephemeral)`. This represents the worst-case concurrent resource usage — all persistent resources are deployed simultaneously, plus the most resource-hungry ephemeral step.
5. Looks up each Kubernetes resource type (e.g., `nvidia.com/gpu`) in the node's `componentValidation.sanity` dict (via `model_dump(by_alias=True)`). If the field exists and `peak_demand > capacity`, raises `ValueError` with the test name, node name, resource type, demand, and capacity. If the field doesn't exist for a resource type, that resource is not validated (capacity is unknown).
6. Uses `parse_k8s_quantity()` in `common.py` to normalize both demand and capacity values to comparable numbers — handles Kubernetes quantity suffixes (`Ki`, `Mi`, `Gi`, `Ti` for binary; `m` for millicores; plain integers and floats).

For node scope, `main.py` calls the validator once per (test, node) pair before calling `compute_node_steps`. For cluster scope, `compute_cluster_steps` in `cluster.py` calls the validator after placement resolution — for `setSize == 1`, once per set against the single node; for `setSize > 1`, once per DAG step against its target node (step *i* validated against node *i* of the set). Project-scoped tests skip resource validation — pods have no target nodes and run wherever the scheduler places them.

**Cluster finally steps** (`compute_teardown_steps` in `step_generator.py`) — teardown steps that run after all tests complete, regardless of success or failure. Each step has `finally_step=True`:

1. generate `create-aggregator` — long-lived Python pod manifest
2. command `create-aggregator` — apply aggregator pod, probe: wait-ready (source: `create-aggregator`)
3. command `aggregate` — exec into aggregator pod to run `aggregate.py`
4. command `cleanup` — delete all pods + services + deployments + configmap

### Manual Writer

`write_manual` in `writers/manual.py` writes steps to `build/manual/`. Manifests go into `manual/manifests/` as data files. Shell scripts go into `manual/` with a `<counter>-` prefix indicating execution order:

1. **Ordering:** Command steps are assigned a counter in execution order, zero-padded to the width of the total step count so that shell glob ordering (`*.sh`) matches execution order. Setup steps get the initial counter values, test steps follow, and teardown steps get the final counter values. Steps that run in parallel across nodes share the same counter.

2. **Writing:** For each step:
   - **Generate steps (manifests):** content is written as `manifests/<name>.yaml` — no counter prefix. These are reference data, not actions.
   - **Command steps (apply):** a shell script is generated that applies the manifest and handles the probe. For `probe: none`: just `oc apply`. For `probe: wait-ready`: apply, then `oc wait --for=condition=Ready` with the step's timeout, then tail recent logs. For `probe: poll-completed`: apply, wait for the pod to start, stream logs in real time with `oc logs -f`, then poll for a terminal phase before checking the result (exits non-zero on failure). Written as `<counter>-<name>.sh`.
   - **Command steps (exec, delete, delete-all):** a shell script is derived from the step config. Written as `<counter>-<name>.sh`. Per-test `finally-teardown` steps are included — they give the operator a single "clean up everything for this test" script, useful when a step fails mid-test.
   - All manual scripts include echo statements so the operator can follow progress without reading the script source.
   - Step names already encode the test_id, test name, node or set index, so no additional prefixing is needed.

3. **Timestamp substitution:** `__TIMESTAMP__` is replaced with the `--run-id` value in all output.

### Tekton Writer

`write_tekton` in `writers/tekton.py` derives Tekton Tasks and Pipelines from the same step list. Generate steps provide the manifest/script content embedded in tasks. Command steps determine the Tekton task type based on `config.command` + `config.probe`:

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
- **Cluster-scoped tests:** one task chain per node set. `setSelection: all` produces one chain per generated set (sequential — each set completes fully before the next begins); `setSelection: random` produces a single chain. Each set is a self-contained DAG cycle. The guard task fans in after the last set's `finally-teardown`.
- **Project-scoped tests:** a single task chain directly in the cluster pipeline, without node affinity.

Every test, regardless of scope, ends with a guard task. The guard task fans in after all the test's `finally-teardown` tasks and serves as the single sync point between tests — the next test's first tasks `runAfter` the guard task. The guard task's `onError` is set according to the test's failure policy (see [Failure Policy Handling](#failure-policy-handling)).

Global finally steps (`finally_step=True`, no `test` — aggregator, cleanup) are placed in the cluster pipeline's `finally` block with `onError: continue`, sequenced via `runAfter`.

All tasks reference the pipeline run name directly via `$(context.pipelineRun.name)`.

#### Failure Policy Handling

The Tekton writer translates each step's failure policy label into `onError` values, `when` guards, and guard tasks.

**Guard tasks:** Every test gets a guard task that fans in after all the test's `finally-teardown` tasks (one chain per node for node-scoped tests, one per set for cluster-scoped, or a single chain for project-scoped). The guard task receives all non-lifecycle task statuses as a comma-separated parameter and exits non-zero if any value is `Failed`. The `onError` on the guard task determines the consequence:

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

- **`skipTest`** — `when` guards on non-first test steps (persistent deploys and ephemeral runs) within each chain, checking `$(tasks.<predecessor>.status) in ["Succeeded"]`. If a test step fails, remaining guarded test steps in that chain are skipped. Lifecycle steps — per-ephemeral cleanup, teardown, and finally-teardown — have no `when` guard and always run. Other chains are unaffected. Guard task with `onError: continue` — pipeline always proceeds to the next test.

- **`abort`** — `when` guards on non-first test steps (same as `skipTest`). All chains complete the current test (pass or fail). Guard task with `onError: stopAndFail` — if any step failed in any chain, the pipeline halts and jumps to cluster `finally`. No further tests run.

When a task is skipped by its `when` guard, its status becomes `None`, causing downstream guarded tasks in the same chain to also skip. The per-chain `finally-teardown` has no `when` guard, so it runs regardless — `scope-when-expressions-to-task` (default since Tekton v0.54) prevents the skip from cascading past unguarded tasks.

#### Test Task Chains

A **task chain** is a linear sequence of Tekton tasks that executes one complete DAG cycle: deploy resources, run tests, collect results, and clean up. Chains are the fundamental unit of execution — every test produces one or more chains, and every chain is self-contained with its own resources, results, and cleanup.

The Tekton writer constructs chains from the flat step list by grouping test-phase steps by their (test_id, node/set) tuple. Within each group, tasks are chained via `runAfter` in step list order. Node-scoped chains for the same test have no `runAfter` between them, so they run in parallel. Cluster-scoped chains are ordered sequentially — each set's first task has a `runAfter` on the previous set's `finally-teardown`. After all chains for a test, the writer inserts a guard task that fans in after every chain's `finally-teardown`, then the next test's first tasks `runAfter` the guard task.

Each test produces one or more task chains placed directly in the cluster pipeline: one per node (node-scoped, parallel), one per set (cluster-scoped, sequential), or one total (project-scoped). Each task uses the same name as the step it corresponds to.

```
node-scoped chain (one of N parallel chains):
  2-inference-wrk-4-vllm-server                          [persistent deploy]
    → 2-inference-wrk-4-pass-fail                         [ephemeral run]
    → 2-inference-wrk-4-cleanup-pass-fail                 [per-ephemeral cleanup]
    → 2-inference-wrk-4-sweep-short-burst                 [ephemeral run (sweep)]
    → 2-inference-wrk-4-cleanup-sweep-short-burst         [per-ephemeral cleanup]
    → 2-inference-wrk-4-sweep-sustained-load              [ephemeral run (sweep)]
    → 2-inference-wrk-4-cleanup-sweep-sustained-load      [per-ephemeral cleanup]
    → 2-inference-wrk-4-teardown                          [teardown]
    → 2-inference-wrk-4-finally-teardown                  [finally-teardown, no when guard]

cluster-scoped chains (setSize: 2, setSelection: all — sequential):
  set0: 3-network-set0-iperf-server                       [persistent deploy]
    → 3-network-set0-iperf-client                         [ephemeral run]
    → 3-network-set0-cleanup-iperf-client                 [per-ephemeral cleanup]
    → 3-network-set0-teardown                             [teardown]
    → 3-network-set0-finally-teardown                     [finally-teardown]
  → set1: 3-network-set1-iperf-server
    → 3-network-set1-iperf-client
    → 3-network-set1-cleanup-iperf-client
    → 3-network-set1-teardown
    → 3-network-set1-finally-teardown
  → ...

project-scoped chain (single, no nodeSelector):
  4-quota-check-runner                                    [ephemeral run]
    → 4-quota-cleanup-check-runner                        [per-ephemeral cleanup]
    → 4-quota-finally-teardown                            [finally-teardown]
```

#### PipelineRun

- Uses `generateName: uat-cluster-run-` (auto-generated unique name per run)
- Sets `spec.timeouts.pipeline` from `config.yaml`'s `pipelineTimeout` (default `7200`, i.e. 2 hours)
- Sets `spec.timeouts.finally` from `config.yaml`'s `finallyTimeout` (default `900`, i.e. 15 minutes) — reserves time for aggregation and cleanup so they run even if the pipeline times out
- The generated name becomes the `$(context.pipelineRun.name)` value referenced by all tasks

## Config Field Usage Map

Every parsed config field and where it takes effect. **This is the section to check when adding or auditing fields.**

### TestSuite (`test_suite.yaml`)

| Field | Model | Effect |
|---|---|---|
| `spec.tests[]` | `TestEntry` (list) | Ordered list of tests to run. List order determines execution order across all scopes |
| `spec.tests[].name` | `TestEntry.name` | Test name — resolves to `<name>.yaml` definition and `<name>.go` source |
| `spec.tests[].scope` | `TestEntry.scope` | One of `node`, `cluster`, `project`. Determines execution pattern: node tests fan out to parallel task chains (one per node), cluster tests produce one chain per node set with placement-controlled distribution (sequential), project tests produce a single chain without node affinity |
| `spec.tests[].onFailure` | `TestEntry.on_failure` | Per-test failure policy (default: `continue`). `continue`: keep executing remaining steps within this test before proceeding to the next. `skipTest`: skip remaining steps in the failing chain (tear down its resources), proceed to the next test. Other chains are unaffected. `abort`: all chains complete the current test (pass or fail), then abort the entire suite |
| `spec.tests[].timeout` | `TestEntry.timeout` | Optional per-test timeout for ephemeral test pod completion polling. Overrides `defaultTestTimeout` from `config.yaml`. If omitted, the default is used |
| `spec.tests[].placement` | `TestEntry.placement` | Cluster scope only. Controls how pods are distributed across nodes. When omitted, defaults produce a single run on one random node |
| `spec.tests[].placement.setType` | `Placement.set_type` | `permutation` (ordered, (A,B) ≠ (B,A)) or `combination` (unordered, {A,B} = {B,A}). Default: `combination` |
| `spec.tests[].placement.setSize` | `Placement.set_size` | Number of distinct nodes per run. When `setSize > 1`, DAG step *i* is placed on node *i* of the set. When `setSize == 1`, all DAG steps share the same node. Default: `1` |
| `spec.tests[].placement.setSelection` | `Placement.set_selection` | `all` generates every set of `setSize` nodes; `random` picks a single random set. Default: `random` |
| `spec.tests[].placement.setRequirements` | `Placement.set_requirements` | Filters eligible nodes by `componentValidation.sanity` fields. Numeric fields are treated as minimums (node value must be ≥ required), string fields as exact matches. Default: empty (all nodes eligible) |
| `spec.tests[].placement.setCutoff` | `Placement.set_cutoff` | Limits the number of sets that run. `0` means no limit. When `setCutoff > 0`, effective count is `min(setCutoff, numSets)`. Ignored when `setSelection` is `random`. Default: `1` |
| `spec.tests[].spec` | `TestEntry.spec` | Optional deep-merge over the test definition's `spec` from `<test>.yaml`. Any field can be overridden, including `serverConfig` and individual DAG step fields. For `dag` overrides, steps are referenced by name as dict keys and only specified fields are overridden — unmentioned fields retain their test.yaml defaults. For all other spec fields, the merge is recursive |

### ClusterTest (`cluster/<name>.yaml`)

| Field | Model | Effect |
|---|---|---|
| `spec.nodes[].name` | `NodeSpec.name` | Node name for `nodeSelector` pinning and step name prefixing. A sanitized version (`NodeSpec.sanitized_name`) is computed at load time for Kubernetes resource names. |
| `spec.nodes[].componentValidation.sanity.*` | `SanityCheck` (extra="allow") | Keys use actual Kubernetes resource names (e.g., `nvidia.com/gpu`, `cpu`, `memory`) for resource validation: the generator compares peak DAG step resource demands against these values. The `resourceNames` sub-dict maps resource keys to hardware model names. Non-resource fields (`nvlink`, `numaNodes`, etc.) are available for component validation checks and Jinja2 templates but are not used for resource validation |
| `spec.nodes[].componentValidation.*` | `ComponentValidation` (extra="allow") | All fields available in Jinja2 templates as `{{ nodeSpec.componentValidation.* }}` |
| `spec.compliance.*` | `ComplianceConfig` | Cluster-wide compliance settings. Consumed by Go test binaries via the embedded `cluster.yaml`, not by the harness itself |
| `spec.namespace` | `ClusterTestSpec.namespace` | Kubernetes namespace for all generated resources |
| `spec.storage.pvc` | `StorageConfig.pvc` | PVC name mounted on all pods (via `subPath` — see PVC Directory Hierarchy) |
| `spec.storage.basePath` | `StorageConfig.base_path` | Root of the directory hierarchy on the PVC: `<basePath>/<timestamp>/<step_name>/`. See PVC Directory Hierarchy |

### Test (`<test-lib>/<test>.yaml`)

| Field | Model | Effect |
|---|---|---|
| `spec.source.ginkgo` | `TestSource` | Path (relative to test library dir) to the Ginkgo test file, read into `LoadedTest`. `go.mod` is generated at build time with the Ginkgo version from `config.yaml` |
| `spec.dag[].persistsThroughSweep` | `DAGStep.persists_through_sweep` | `true`: rendered as generate + command (apply, wait-ready) pod (+ service); stays up for all sweep entries. `false`: rendered as generate + command (apply, poll-completed) pod; one per sweep entry |
| `spec.dag[].service` | `DAGStep.service` | If `enabled: true`, generates a Service manifest and populates `{{ services["name"].url }}` in template context. `headless: true` (default) creates a headless Service (ClusterIP: None) |
| `spec.dag[].command` | `DAGStep.command` | Structured command: `args` + `flags` → `["arg1", "--key=value"]`. Both persistent and non-persistent steps render command args through the Jinja2 template context (`serverConfig`, `nodeSpec`, `services`, `node`, `timestamp`). Non-persistent steps additionally have `paramSweep` available |
| `spec.dag[].labelFilter` | `DAGStep.label_filter` | If set, takes priority over `command`: generates a ginkgo command with `--ginkgo.label-filter=<value>` and `--ginkgo.junit-report=/workspace/junit.xml`. Also auto-injects `RESULTS_DIR` env var if not already present |
| `spec.dag[].parameterSweep` | `DAGStep.parameter_sweep` | If set: one test pod per `entries[]`. Each entry's `flags` are merged over `baseCommand.flags`. If null: single test pod using the step's own command |
| `spec.dag[].env` | `DAGStep.env` | Env vars. Values are rendered through Jinja2 with the full template context |
| `spec.dag[].resources` | `DAGStep.resources` | Resource requests/limits. Values are rendered through Jinja2 with the full template context (`nodeSpec`, `serverConfig`, `services`, `node`, `timestamp`), so expressions like `{{ nodeSpec.componentValidation.sanity["nvidia.com/gpu"] }}` work in both persistent and non-persistent steps. |
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
| `builderTimeout` | `ToolConfig.builder_timeout` | Timeout for builder pod readiness probe, integer seconds (default `300`) |
| `aggregatorTimeout` | `ToolConfig.aggregator_timeout` | Timeout for aggregator pod readiness probe, integer seconds (default `120`) |
| `deployTimeout` | `ToolConfig.deploy_timeout` | Timeout for DAG pod readiness probes, integer seconds (default `600`) |
| `defaultTestTimeout` | `ToolConfig.default_test_timeout` | Default timeout for test pod completion polling, integer seconds (default `600`). Can be overridden per-test via `timeout` in `test_suite.yaml` |
| `pipelineTimeout` | `ToolConfig.pipeline_timeout` | Sets `spec.timeouts.pipeline` on the PipelineRun manifest, integer seconds (default `7200`) |
| `finallyTimeout` | `ToolConfig.finally_timeout` | Sets `spec.timeouts.finally` on the PipelineRun manifest — reserves time for aggregation and cleanup after pipeline timeout, integer seconds (default `900`) |

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

Every DAG step gets a unique directory on the PVC, named after the step. The step name encodes all hierarchy information (test_id, test name, node or set index, DAG step), so directories are flat under the timestamp. Test authors do not specify paths — they write to `/workspace` and files land in the right place.

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

Concrete example with `basePath=uat/results`, two node-scoped tests (component, inference), a cluster-scoped test, and a project-scoped test:

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
  3-network-set0-iperf-server/             ← cluster-scoped, set 0
  3-network-set0-iperf-client/
    junit.xml
  3-network-set1-iperf-server/             ← cluster-scoped, set 1
  3-network-set1-iperf-client/
    junit.xml
  ...
  4-quota-check-runner/                    ← project-scoped (no node segment)
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
| Cluster (multiple sets) | `<basePath>/__TIMESTAMP__/<test_id>-<test>-set<i>-<dag_step>` |
| Cluster (multiple sets, with sweep) | `<basePath>/__TIMESTAMP__/<test_id>-<test>-set<i>-<dag_step>-<id>` |
| Cluster (single set) | `<basePath>/__TIMESTAMP__/<test_id>-<test>-<dag_step>` |
| Cluster (single set, with sweep) | `<basePath>/__TIMESTAMP__/<test_id>-<test>-<dag_step>-<id>` |
| Project | `<basePath>/__TIMESTAMP__/<test_id>-<test>-<dag_step>` |
| Project (with sweep) | `<basePath>/__TIMESTAMP__/<test_id>-<test>-<dag_step>-<id>` |

The `__TIMESTAMP__` placeholder is substituted by each writer: the manual writer replaces it with `--run-id`, the Tekton writer replaces it with a pipeline-runtime expression that resolves to the run name.

### Pod Volume Mounting

Each pod type mounts the PVC with a `subPath` scoped to its role. DAG pods also get a second mount at `/binaries` for access to compiled test binaries.

| Pod type | `/workspace` | `/binaries` | Notes |
|---|---|---|---|
| Builder | subPath: `<basePath>/<ts>/binaries` | — | Writes to `/workspace/<test>/test.bin` |
| Aggregator | subPath: `<basePath>/<ts>` | — | Scans step directories for `junit.xml` |
| Persistent DAG pod | subPath: `<basePath>/<ts>/<step_name>` | subPath: `<basePath>/<ts>/binaries` | Server logs, model cache written to `/workspace` |
| Ephemeral test pod | subPath: `<basePath>/<ts>/<step_name>` | subPath: `<basePath>/<ts>/binaries` | `junit.xml` written to `/workspace` |

Because `/workspace` IS the step's unique directory:
- Test pods write `junit.xml` to `/workspace/junit.xml`
- Ginkgo binaries are accessed at `/binaries/<test>/test.bin`
- Benchmark tools use `output-dir: /workspace`

## Pod Name Conventions

Pod and service names use the step's `resource_name`, which encodes test_id, test name, and sanitized node name (for node-scoped tests) or set index (for cluster-scoped multi-set tests) to avoid collisions. `<node>` below refers to the sanitized node name:

| Resource | Name pattern | Example |
|---|---|---|
| Persistent DAG pod (node) | `<test_id>-<test>-<node>-<dag_step.name>` | `2-inference-wrk-4-vllm-server` |
| Service (node) | `svc-<test_id>-<test>-<node>-<service.name>` | `svc-2-inference-wrk-4-vllm-server` |
| Test pod (node) | `<test_id>-<test>-<node>-<dag_step.name>` | `1-component-wrk-4-test-runner` |
| Test pod (node, sweep) | `<test_id>-<test>-<node>-<dag_step.name>-<id>` | `2-inference-wrk-4-sweep-short-burst` |
| Persistent DAG pod (cluster, multiple sets) | `<test_id>-<test>-set<i>-<dag_step.name>` | `3-network-set0-iperf-server` |
| Service (cluster, multiple sets) | `svc-<test_id>-<test>-set<i>-<service.name>` | `svc-3-network-set0-iperf-server` |
| Test pod (cluster, multiple sets) | `<test_id>-<test>-set<i>-<dag_step.name>` | `3-network-set0-iperf-client` |
| Test pod (cluster, multiple sets, sweep) | `<test_id>-<test>-set<i>-<dag_step.name>-<id>` | `3-network-set0-iperf-client-short-burst` |
| Persistent DAG pod (cluster single set / project) | `<test_id>-<test>-<dag_step.name>` | `3-network-iperf-server` |
| Service (cluster single set / project) | `svc-<test_id>-<test>-<service.name>` | `svc-3-network-iperf-server` |
| Test pod (cluster single set / project) | `<test_id>-<test>-<dag_step.name>` | `4-quota-check-runner` |
| Test pod (cluster single set / project, sweep) | `<test_id>-<test>-<dag_step.name>-<id>` | `4-quota-check-runner-short-burst` |
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
| `nodeSpec.*` | Full node spec from cluster config | `{{ nodeSpec.componentValidation.sanity["nvidia.com/gpu"] }}` |
| `services["name"]` | Service context from DAG steps with `service.enabled` | `{{ services["vllm-server"].url }}` |
| `timestamp` | `__TIMESTAMP__` placeholder | Replaced at output time |
| `node` | Node name | `wrk-4` |


## Call Graph

```
__main__.py → main()                                               [src/main.py]

main()
├── if --steps:
│   ├── load_steps_file(path)                                       [src/step_generator.py]
│   │   Loads steps from a previously written steps.json (skips computation)
│   ├── _validate_unique_pod_names(steps)                           [src/step_generator.py]
│   └── _validate_service_names(steps)                              [src/step_generator.py]
│
├── else:
│   ├── load_tool_config(config_path)                               [src/common.py]
│   ├── load_config(suite_path, lib_dir, cluster_path)              [src/common.py]
│   │
│   ├── compute_setup_steps(...)                                    [src/step_generator.py]
│   │   Produces generate + command steps for configmap, builder pod, build
│   │
│   ├── compute_node_steps(...)                                     [src/node.py]
│   │   Per node, per test: produces generate + command steps for
│   │   DAG deployment, test execution, and teardown
│   │
│   ├── compute_cluster_steps(...)                                  [src/cluster.py]
│   │   Per set, per test: placement resolution, set generation,
│   │   nodeSelector assignment, DAG deployment, test execution, and teardown
│   │
│   ├── compute_project_steps(...)                                  [src/project.py]
│   │   Single chain per test: DAG deployment, test execution, and teardown
│   │   (no node affinity)
│   │
│   ├── compute_teardown_steps(...)                                 [src/step_generator.py]
│   │   Produces generate + command steps for aggregator pod, aggregation, cleanup
│   │
│   ├── _validate_unique_pod_names(steps)                           [src/step_generator.py]
│   │   Validates pod names for RFC 1123 compliance and uniqueness
│   │
│   ├── _validate_service_names(steps)                              [src/step_generator.py]
│   │   Validates service names for DNS-1035 compliance
│   │
│   └── write_steps_file(...)                                       [src/step_generator.py]
│       Serializes steps to steps.json for round-tripping
│
├── write_manual(...)                                               [src/writers/manual.py]
│   Generate steps → write manifests to manifests/ (no counter); command steps → derive numbered shell scripts
│
└── write_tekton(...)                                               [src/writers/tekton.py]
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
| `TestSuite` | `test_suite.yaml` | `spec.tests[]` — ordered list of `TestEntry` (name, scope, onFailure, timeout, placement, spec) |
| `Test` | `<test>.yaml` | `spec.dag[]`, `spec.source`, `spec.serverConfig` |
| `DAGStep` | nested in `Test` | `name`, `image`, `command`, `env`, `service`, `ports`, `readinessProbe`, `resources`, `volumeMounts`, `volumes`, `privileged`, `persistsThroughSweep`, `parameterSweep`, `labelFilter` |
| `ParameterSweep` | nested in `DAGStep` | `baseCommand.{args,flags}`, `entries[].{id,description,flags}` |
| `ClusterTest` | `cluster/*.yaml` | `spec.nodes[]`, `spec.namespace`, `spec.storage.{pvc,basePath}`, `spec.compliance` |
| `NodeSpec` | nested in `ClusterTest` | `name`, `componentValidation.sanity.*` (all via `extra="allow"`, keys use K8s resource names) |
| `ToolConfig` | `config.yaml` | `oseCLIImage`, `builderImage`, `ginkgoVersion`, `aggregatorImage`, `configmapName`, `builderPodName`, `aggregatorPodName`, `nodeSelectorKey`, `managedByLabel`, `builderTimeout`, `aggregatorTimeout`, `deployTimeout`, `defaultTestTimeout`, `pipelineTimeout`, `finallyTimeout` |
| `LoadedTest` | (dataclass) | `name`, `spec: TestSpec`, `go_source`, `on_failure`, `timeout`, `test_id`, `scope`, `placement` |
| `Step` | (dataclass) | `name`, `type` (`generate` or `command`), `config` (type-specific: `output`/`command`/`probe`/`timeout`), `content` (generate only), `source` (command only, list of generate step names), `resource_name` (sanitized name for Kubernetes resources; equals `name` when already RFC 1123 compliant), `node` (node name, empty for global steps), `test` (test name, empty for setup/teardown), `test_id` (1-indexed position in test suite, empty for setup/teardown), `on_failure` (test policy: `continue`/`skipTest`/`abort`, empty for setup/teardown), `finally_step` (if `true` and no `test`: placed in cluster pipeline `finally` block; if `true` and has `test`: rendered as regular task with no `when` guard), `lifecycle` (`true` for cleanup, teardown, and finally-teardown steps — no `when` guards, excluded from guard task status checks), `scope`, `phase` |
| `StepsFile` | `steps.json` | `metadata` (must contain `toolConfig`, `clusterSpec`, and `setMappings` for cluster-scoped tests recording which nodes are in each set keyed by `test_id`), `steps[]` — flat list of serialized steps. Validated on load: step structure, source references, pod name uniqueness, and failure policy labels |

## Resource Validation

For node-scoped and cluster-scoped tests, `validate_node_resources` in `common.py` computes peak concurrent resource demand per target node (sum of persistent + max of ephemeral DAG step resource requests) and compares against the node's `componentValidation.sanity` fields. Sanity dict keys use actual Kubernetes resource names (e.g. `nvidia.com/gpu`, `cpu`, `memory`) so they match resource requests directly. The generator aborts with an error if any resource type demand exceeds the node's declared capacity. This catches over-subscription (e.g., two GPU-hungry DAG steps on a 4-GPU node) at generation time. Project-scoped tests skip this check — pods have no specific target nodes.

For cluster-scoped tests, `setRequirements` in the placement config filters nodes by comparing requirement values against the sanity dict. Numeric values check `>=`, string values check exact match.

## Known Constraints

- **ConfigMap 1MB limit:** All Go source, cluster config, test suite config, build script, and aggregator script are packed into a single ConfigMap. A project with many tests may exceed Kubernetes' 1MB ConfigMap limit.
- **Resource name length**: step and task names are constructed by concatenating test_id, test name, node or set index, and DAG step (e.g. `2-inference-wrk-4-vllm-server`, `3-network-set0-iperf-server`). Node names are capped at 16 characters (12 + 4-char hash if longer), but the full resource name can still exceed the 63-character Kubernetes name limit with long test or DAG step names.
- **One cluster pipeline per namespace**: the builder pod has a fixed name, so only one cluster pipeline can run at a time in a given namespace. This is typically sufficient — the task chains are the element that scales with cluster size, and a single cluster pipeline fans out to all target nodes in parallel.
- **Sequential sweeps**: parameter sweep entries within a test run as separate pods in sequence. Failure behavior is controlled per-test via the `onFailure` field in `test_suite.yaml` (`continue`, `skipTest`, or `abort`). All three policies produce a guard task between tests. `continue` uses no `when` guards, so all tasks run through failures. `skipTest` adds `when` guards that skip remaining test steps in the chain after a failure. Both use `onError: continue` on the guard task, so the next test always proceeds. `abort` uses the same `when` guards as `skipTest`, but the guard task uses `onError: stopAndFail` — halting the pipeline if any chain had a failure. In manual mode, scripts are independent and the operator controls whether to proceed.
- **Combinatorial growth for cluster tests**: `setSelection: all` generates P(n, k) sets for permutations or C(n, k) for combinations, where n is the number of eligible nodes and k is `setSize`. Each set runs as a complete DAG cycle. For large clusters with `setType: permutation` and high `setSize`, the number of sets grows factorially — e.g. 10 nodes with `setSize: 3` produces 720 permutations. Use `setSelection: random` or `setType: combination` (which produces 120 for the same parameters) to bound the run count.
