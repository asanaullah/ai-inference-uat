<!-- Assisted by Claude Opus -->
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
  │                         persistent/ephemeral/resource/teardown step rendering
  node.py                ← node-level step computation, DAG/test pod rendering,
  │                         requirement checks
  cluster.py             ← cluster-level step computation, placement resolution
  │                         (set generation, node filtering, nodeSelector assignment)
  project.py             ← project-level step computation (single sequence, no node affinity)
  models.py              ← Pydantic schemas + dataclasses (no internal deps)
  writers/
    manual.py            ← manual writer (numbered shell scripts + YAML manifests)
scripts/
  aggregate.py           ← JUnit XML aggregation script (deployed via ConfigMap)
  manual_runner.py       ← interactive driver that steps through build/manual/ scripts
  auto_runner.py         ← headless driver that runs a suite unattended from build/manual/
templates/
  *.yaml.j2              ← Jinja2 templates for all Kubernetes manifests
  *.sh.j2                ← Jinja2 templates for shell scripts
```

**Dependency graph:** `main.py` → `common.py`, `models.py`, `node.py`, `cluster.py`, `project.py`, `step_generator.py`, `writers/manual.py`. `step_generator.py` → `common.py`, `models.py`. `writers/manual.py` → `common.py`, `models.py`. `node.py` → `common.py`, `models.py`. `cluster.py` → `common.py`, `models.py`. `project.py` → `common.py`, `models.py`. `common.py` → `models.py`. `models.py` has no internal deps.

## Compute / Write Architecture

The generator is invoked as `python -m src`. Full CLI:

| Flag | Default | Required | Description |
|---|---|---|---|
| `--test-suite` | — | Yes (unless `--steps`) | Path to the test suite YAML (see `examples/all_tests.yaml`) |
| `--test-lib` | — | Yes (unless `--steps`) | Directory containing `<test>.yaml` and `<test>.go` files |
| `--cluster` | — | Yes (unless `--steps`) | Path to the cluster config |
| `--config` | `config.yaml` | No | Path to `config.yaml` (ToolConfig) |
| `--steps` | — | No | Path to a previously written `steps.json` — skips step computation, re-runs only the writer |
| `--run-id` | `manual-run` | No | Timestamp substitution value for manual output |
| `--output` | `build` | No | Output directory root (the manual writer creates `manual/` under this) |
| `--scripts-dir` | `scripts` | No | Directory containing `aggregate.py` (bundled into the ConfigMap) |
| `--templates-dir` | `templates` | No | Directory containing Jinja2 templates (`.yaml.j2`, `.sh.j2`) |

`main()` in `main.py` branches on `--steps`: if provided, it loads the step list from `steps.json` via `load_steps_file()`, re-validates pod and service names, and proceeds directly to the writer. Otherwise, it loads the three input files, computes steps, validates, serializes to `steps.json`, and then runs the writer.

**Error handling:** `main()` wraps each phase in targeted exception handlers. Template engine initialization catches `OSError` and `TemplateError`. Steps file loading catches `FileNotFoundError`, `json.JSONDecodeError`, `ValidationError`, and `ValueError`. The writer catches `OSError`, `TemplateError`, and `ValueError`. All caught exceptions print a descriptive error message and raise `SystemExit(1)` — the generator never produces partial output on failure.

The generator separates **what to run** (step computation) from **how to run it** (the writer). Step computation produces a single ordered list of steps — the complete specification of every resource and action needed for the test suite. A writer is an independent consumer that translates the step list into an execution format. Adding a new execution backend (e.g. Argo Workflows, GitHub Actions) means writing a new writer — step computation doesn't change.

All steps are computed with `__TIMESTAMP__` as a literal placeholder in any path or value that needs run-level isolation (results directories, aggregator paths). The manual writer substitutes it with a user-provided `--run-id` value.

```
Step computation → [Step list] ─────→ Manual writer → build/manual/ (__TIMESTAMP__ → run-id)
```

### Step Computation

Produces a flat list of `Step` dataclasses. Each step is one of two types:

**Generate step** — produces an artifact (Kubernetes manifest or in-pod script):

- `name` — human-readable identity, referenced by command steps via `source` (used for manual script/manifest filenames and PVC directory names)
- `type` — `'generate'`
- `resource_name` — fixed-width Kubernetes `metadata.name` (pods, services, arbitrary resources) produced by `build_resource_name()`. Always DNS-1035 valid and bounded in length (see [Resource Naming](#resource-naming))
- `config.output` — `'manifest'` or `'script'`
- `content` — rendered manifest/script text

**Command step** — represents an action to execute:

- `name` — human-readable identity
- `type` — `'command'`
- `resource_name` — fixed-width Kubernetes resource name (from `build_resource_name()`) used for `metadata.name` and resource references
- `config.command` — `'apply'`, `'exec'`, `'delete'`, `'delete-all'`
- `config.probe` — `'wait-ready'`, `'poll-completed'`, `'none'`
- `config.timeout` — for probe wait logic
- `config.pod_name` — target pod for apply+wait-ready / apply+poll-completed steps (also used for uniqueness validation)
- `config.target` — pod to exec into (exec steps only)
- `config.args` — command arguments (exec steps only)
- `config.selector` — label selector for delete steps (e.g. `test=guidellm,node=wrk-4,sweep=pass-fail`)
- `config.configmap_name` — ConfigMap name for delete-all steps
- `config.managed_by_label` — managed-by label value for delete-all steps
- `config.service_name` — service name for generate steps with an associated Service (used for DNS-1035 validation)
- `source` — list of generate step names whose content to use

**Step-level fields** (set during computation, used by the writer):

- `phase` — `'setup'`, `'test'`, or `'teardown'`. The writer groups steps by phase to separate setup, per-test, and teardown output.
- `scope` — `'node'`, `'cluster'`, or `'project'` (empty for setup/teardown). Determines the execution pattern the writer emits (parallel per node, sequential node sets, or a single sequence).
- `finally_step` — marks steps that must run regardless of earlier failures. If `true` and the step has no `test` (global finally — aggregator, cleanup), it runs after all tests complete. If `true` and the step has a `test` (per-test finally-teardown), it is the last step in the test's sequence and runs even when earlier steps fail. The runner honors this flag by always executing these steps.
- `lifecycle` — `true` for automatically generated lifecycle steps (per-ephemeral cleanup, teardown, and finally-teardown). These steps always run regardless of failure policy, and their statuses are excluded from the runner's pass/fail check for the test. This flag distinguishes test steps (subject to failure policy) from lifecycle steps (which always run).
- `namespace` — target Kubernetes namespace for this step. Set during computation: default-namespace steps get `cs.namespace`, peer-flagged DAG steps get `cs.peer_namespace`. The writer resolves the effective namespace as `step.namespace or default_namespace`, so if `namespace` is empty (legacy steps), the cluster-level default is used.

**Failure policy labelling** — each step carries the test's `on_failure` policy from the test suite (`continue`, `skipTest`, or `abort`). The runner reads this label off each step and enforces the policy after every test (see [Failure Policy Handling](#failure-policy-handling)).

The step list is built in three sections — setup, per-test, and teardown:

**Setup steps** (`compute_setup_steps` in `step_generator.py`). All setup steps carry `namespace=cs.namespace`:

1. generate `apply-configmap` — ConfigMap manifest with all Go source, cluster.yaml, test_suite.yaml, build.sh, aggregate.py
2. command `apply-configmap` — apply configmap (source: `apply-configmap`)
3. generate `create-builder` — long-lived Go toolchain pod manifest
4. command `create-builder` — apply builder pod, probe: wait-ready (source: `create-builder`)
5. command `build` — exec into builder pod to run `build.sh`

Binaries are compiled once per test name and stored at `binaries/<test_name>/test.bin`, not per `test_id`. If the same test appears multiple times in the test suite, all instances share the same `<test>.go` source file — they differ only in runtime config (`onFailure`, `timeout`, sweep parameters), not in compiled code.

**Scope validation:** After loading each test definition, `load_config()` checks the suite entry's `scope` against the test definition's `metadata.supportedScopes` list. If the scope is not in the list, it raises `ValueError` with the test name, requested scope, and supported scopes. This catches scope mismatches at load time rather than during step computation.

**Spec override resolution:** Before step computation, `load_config()` in `common.py` loads each test definition from `<test>.yaml` in the test library. If a `TestEntry` in the test suite includes a `spec` section, it is deep-merged over the loaded test definition's `spec`. The merge is recursive for dict fields (`serverConfig`) — nested keys are merged, not replaced. For `dag` overrides, the suite entry uses DAG step names as dict keys (not a list): each key is matched to a DAG step by `name`, and only the specified fields within that step are overridden — unmentioned fields and unmentioned DAG steps retain their `<test>.yaml` defaults. After merging, the result is re-validated by constructing a new `TestSpec` from the merged dict — this catches invalid types, unknown fields, or malformed DAG step definitions introduced by the suite-level override. Without re-validation, such errors would only surface during step computation or manifest rendering with less clear error messages. The validated `TestSpec` becomes the `LoadedTest.spec` used for all subsequent step computation. This allows the same test definition in the test library to produce different runtime configurations across suite entries without duplicating the test file.

**Test steps**: per-test, with scope determining the execution pattern. Each scope has its own step computation function. Each step carries **two names** (see [Resource Naming](#resource-naming)): a human-readable `name` and a fixed-width `resource_name`. The human-readable `name` follows the convention `<test_id>-<test>-<node>-<dag_step.name>` for node scope, `<test_id>-<test>-<set>-<dag_step.name>` for cluster scope with multiple sets, and `<test_id>-<test>-<dag_step.name>` for cluster scope with a single set or project scope, with `-<id>` appended for sweep entries. `<test_id>` is a zero-padded 3-digit 1-indexed position of the test in the test suite list (e.g. `001`, `002`); `<set>` is a zero-padded 4-digit set index (e.g. `0000`, `0001`). The same test can appear multiple times in the list (e.g. with different configs or failure policies), so `<test_id>` prevents collisions in resource names and results paths, while `<test>` provides readability. The common pattern across scopes:

1. For each resource DAG step (has `resourceConfig`): generate manifest (arbitrary K8s resource rendered via `resource.yaml.j2`) + command to apply it. The resource type (e.g. `InferencePool`) is tracked and appended to the teardown resource type list
2. For each persistent pod DAG step: generate manifest (pod + optional service) + command to deploy and wait for readiness. Service `metadata.name` is the `svc`-typed `build_resource_name()` output (DNS-1035 valid); service URL references in env vars and commands resolve to the same name via the `services` template context
3. For each non-persistent pod DAG step (one per sweep entry, or one if no sweep): generate manifest + command to run and poll for completion + command to delete pods and services by sweep label. The `sweep` label value is the sweep entry's `id` for sweep steps, or the DAG step's `name` for non-sweep steps
4. If test had persistent or resource steps: command to tear down persistent resources (resource type list is `pods,services` plus any resource step types like `InferencePool`)
5. command `<test_id>-<test>[-<node>|-<set>]-finally-teardown` (delete by label, `finally_step=True`) — always generated for every test

#### Resource Naming

Every step has two names:

- **Step name** (`Step.name`) — human-readable, e.g. `002-guidellm-wrk-4-vllm-server`. Used for manual script/manifest filenames, PVC workspace directory names, and results paths.
- **Resource name** (`Step.resource_name`) — the Kubernetes `metadata.name`, produced by `build_resource_name()` in `common.py`. Always DNS-1035 valid and bounded in length.

`build_resource_name()` produces a fixed-width, positional, dash-padded name:

```
ua-<tid:3>-<type:3>-<step:16>-<node:10>-<set:4>-<sweep:8>-t     (pods, services, cleanup, teardown)
ua-<tid:3>-<type:3>-<step:16>-<set:4>-t                          (crd form — omits node and sweep)
```

Each field is lowercased, RFC 1123-sanitized, and either truncated or right-padded with `-` to its fixed width, so the total length is always constant (54 chars for the pod form, 34 for the crd form) and every position is stable regardless of input. `fit(value, width)` performs the per-field transform: it lowercases and sanitizes, and if the result exceeds `width` it truncates to `width - 5` and appends a 4-char SHA-256 hash (so long values stay unique). `test_id` and `set_key` use `fit(..., strict=True)` — they must already be within width (enforced by the 999-test / 9999-set caps) or generation aborts.

The 3-char `type` code identifies the resource kind:

| Code | Resource |
|---|---|
| `pod` | DAG / test pod |
| `svc` | Service |
| `crd` | Arbitrary resource (`resourceConfig`) |
| `cln` | Per-ephemeral cleanup |
| `tdn` | Teardown |
| `ftd` | Finally-teardown |

`bld`, `agg`, and `cfg` are reserved in `_RESOURCE_TYPES` but currently unused (the builder, aggregator, and ConfigMap use fixed names from `config.yaml`).

Example: the step named `002-guidellm-wrk-4-vllm-server` gets resource name `ua-002-pod-vllm-server------wrk-4--------------------t`.

**Peer namespace routing:** All three scope-level compute functions (`compute_node_steps`, `compute_cluster_steps` via `_generate_set_steps`, `compute_project_steps`) accept `peer_namespace`, `peer_pvc`, `peer_base_path`, and `peer_models_storage` parameters. For each DAG step, if `dag_step.peer` is `True`, the step's `namespace`, PVC, base path, and models storage are resolved to the peer variants (falling back to the default if the peer variant is unset). Each step's `Step.namespace` field is set to the resolved namespace, which the writer uses for `oc` commands.

Persistent and resource state is tracked separately for default and peer namespaces (`has_persistent`/`has_peer_persistent`, `extra_resource_types`/`extra_peer_resource_types`). After the DAG loop, `add_teardown_steps()` is called once for the default namespace. If any DAG step was peer-flagged (`has_peer` is `True` and `peer_namespace` is set), a second `add_teardown_steps()` call generates peer teardown steps with `-peer` suffixed names (e.g. `<test_id>-<test>-<node>-peer-teardown`, `<test_id>-<test>-<node>-peer-finally-teardown`) targeting the peer namespace.

**Node scope** (`compute_node_steps` in `node.py`): steps are generated per-node, per-test. Labels include `node=<node>` for targeted cleanup.

1. For each resource DAG step: generate `<test_id>-<test>-<node>-<dag_step>` manifest (arbitrary K8s resource) + command (apply, probe: none)
2. For each persistent pod DAG step: generate `<test_id>-<test>-<node>-<dag_step>` manifest (pod + optional service, joined with `---`) + command `<test_id>-<test>-<node>-<dag_step>` (apply, probe: wait-ready)
3. For each non-persistent pod DAG step: generate manifest + command (apply, probe: poll-completed) + command `<test_id>-<test>-<node>-cleanup-<dag_step>[-<id>]` (delete by label)
4. If test had persistent or resource steps: command `<test_id>-<test>-<node>-teardown` (delete by label)
5. command `<test_id>-<test>-<node>-finally-teardown` (delete by label, `finally_step=True`)
6. If test had peer steps: command `<test_id>-<test>-<node>-peer-teardown` (if peer had persistent/resource steps) + command `<test_id>-<test>-<node>-peer-finally-teardown` (delete by label, `finally_step=True`, `namespace=peer_namespace`)

**Cluster scope** (`compute_cluster_steps` in `cluster.py`): placement is fully resolved during step computation. The function proceeds in three phases:

**Phase 1 — Node filtering:** If the suite entry's `placement.setRequirements` is non-empty, each node from the cluster config is checked against the requirements. Numeric fields in `componentValidation.sanity` are treated as minimums (node value must be ≥ required), string fields as exact matches. Nodes that fail any requirement are excluded. If no nodes pass, no steps are generated.

**Phase 2 — Set generation:** From the filtered node list, sets of size `placement.setSize` are generated. `setType: permutation` generates ordered tuples (using `itertools.permutations`), where (A,B) and (B,A) are distinct sets. `setType: combination` generates unordered groups (using `itertools.combinations`), where {A,B} = {B,A}. `setSelection: random` picks a single random set from the generated list. `setSelection: all` uses all generated sets, clamped by `setCutoff` if non-zero (`min(setCutoff, len(sets))`).

**Phase 3 — Step generation:** For each set, steps are generated following the same lifecycle as a node-scoped sequence. The number of sequences is always resolved algorithmically from placement config — Phase 2 determines how many sets survive filtering, selection, and cutoff, and each surviving set becomes exactly one sequence. Sets are ordered sequentially in the step list — each set's steps follow the previous set's `finally-teardown`. Multi-set runs include a 4-digit `<set>` segment in step names (e.g. `0000`, `0001`); single-set runs omit it. Multi-set pods and services carry a `chain` label with the set key (e.g., `chain=0000`), and cleanup selectors include this label to scope teardown to the current set — preventing leakage between sets if a teardown fails. Single-set runs omit the `chain` label since `test=<name>` is sufficient with only one set.

The `setSize` determines how nodeSelectors are assigned within each sequence:

- **`setSize == 1`**: all DAG steps in the sequence share the same node (the single node in the set). The sequence structure is identical to a node-scoped sequence — the only difference is naming (no `<node>` segment, optional `<set>` segment) and that the node was selected by placement config rather than fan-out.

- **`setSize > 1`**: the number of DAG steps must equal `setSize` — the generator validates this and aborts if they don't match. DAG step *i* gets a `nodeSelector` pinning it to node *i* of the set. This means different steps within the same sequence run on different nodes (e.g., a server on node A, a client on node B). The lifecycle is the same (persistent deploy, ephemeral run, per-ephemeral cleanup, teardown, finally-teardown), but resources are distributed across the set's nodes rather than colocated.

1. For each resource DAG step: generate `<test_id>-<test>-[<set>-]<dag_step>` manifest (arbitrary K8s resource) + command (apply, probe: none)
2. For each persistent pod DAG step: generate `<test_id>-<test>-[<set>-]<dag_step>` manifest (pod + optional service) + command (apply, probe: wait-ready)
3. For each non-persistent pod DAG step: generate manifest + command (apply, probe: poll-completed) + command `<test_id>-<test>-[<set>-]cleanup-<dag_step>[-<id>]` (delete by label)
4. If test had persistent or resource steps: command `<test_id>-<test>-[<set>-]teardown` (delete by label)
5. command `<test_id>-<test>-[<set>-]finally-teardown` (delete by label, `finally_step=True`)
6. If test had peer steps: command `<test_id>-<test>-[<set>-]peer-teardown` (if peer had persistent/resource steps) + command `<test_id>-<test>-[<set>-]peer-finally-teardown` (delete by label, `finally_step=True`, `namespace=peer_namespace`)

The `setMappings` metadata (recording which nodes are in each set, keyed by `test_id`) is written to `steps.json` by `write_steps_file()` — useful for `random` selection where the chosen set is non-deterministic.

**Project scope** (`compute_project_steps` in `project.py`): produces a single sequence without node affinity. No placement resolution or node filtering — the function takes the test definition and generates steps directly, without iterating over nodes or sets. Pods are rendered without `nodeSelector`, so the Kubernetes scheduler places them freely. Step names follow the convention `<test_id>-<test>-<dag_step>`. Labels include only the test-level identifiers (no node or set labels), so cleanup targets all resources for the test. The step generation pattern is identical to a single node-scoped sequence, minus the node segment in names and the `nodeSelector` in manifests.

1. For each resource DAG step: generate `<test_id>-<test>-<dag_step>` manifest (arbitrary K8s resource) + command (apply, probe: none)
2. For each persistent pod DAG step: generate `<test_id>-<test>-<dag_step>` manifest (pod + optional service) + command (apply, probe: wait-ready)
3. For each non-persistent pod DAG step: generate manifest + command (apply, probe: poll-completed) + command `<test_id>-<test>-cleanup-<dag_step>[-<id>]` (delete by label)
4. If test had persistent or resource steps: command `<test_id>-<test>-teardown` (delete by label)
5. command `<test_id>-<test>-finally-teardown` (delete by label, `finally_step=True`)
6. If test had peer steps: command `<test_id>-<test>-peer-teardown` (if peer had persistent/resource steps) + command `<test_id>-<test>-peer-finally-teardown` (delete by label, `finally_step=True`, `namespace=peer_namespace`)

**Node name handling:** Node names are fit into the fixed-width `node` field (10 chars) of every resource name and into the `node` label via `fit(node, 10)` — lowercased, RFC 1123-sanitized, and truncated to 5 chars + a 4-char hash if longer. The original node name is used for `nodeSelector` values, human-readable step names, manual script filenames, and PVC directory paths. (`sanitize_node_name`/`NodeSpec.sanitized_name` are legacy and no longer used by name construction — `fit()` handles all truncation.)

**Name validation:** After all steps are computed, `_validate_unique_pod_names()` in `step_generator.py` validates pod names for RFC 1123 label compliance and uniqueness. Uniqueness is scoped by namespace: the validator uses `(pod_name, step.namespace)` tuples, so the same pod name in different namespaces (e.g. default and peer) does not collide. `_validate_service_names()` validates service names for DNS-1035 compliance (must start with a lowercase letter, contain only lowercase alphanumeric characters and hyphens, and end with a lowercase alphanumeric character). The generator aborts with an error if any validation fails.

**Resource validation:** Before step computation for each node-scoped or cluster-scoped test, the generator validates that each target node has sufficient resources for the test's peak concurrent demand. `validate_node_resources(test, node_spec, jinja_env)` in `common.py` performs the check:

1. Builds a minimal Jinja2 render context: `{"nodeSpec": node_spec_dict, "serverConfig": test.spec.server_config}`.
2. For each DAG step with `resources.requests`, renders each value through Jinja2 to resolve template expressions (e.g., `{{ nodeSpec.componentValidation.sanity["nvidia.com/gpu"] }}` → `4`).
3. Classifies each pod DAG step as persistent (`persistsThroughSweep: true`) or ephemeral (default). Resource steps (those with `resourceConfig`) are skipped — they don't produce pods and have no resource requests. Each ephemeral DAG step contributes one entry to the ephemeral demand list (sweep entries share the same resource requests, so they produce the same demand).
4. Aggregates per resource type: `peak_demand = sum(persistent) + max(ephemeral)`. This represents the worst-case concurrent resource usage — all persistent resources are deployed simultaneously, plus the most resource-hungry ephemeral step.
5. Looks up each Kubernetes resource type (e.g., `nvidia.com/gpu`) in the node's `componentValidation.sanity` dict (via `model_dump(by_alias=True)`). If the field exists and `peak_demand > capacity`, raises `ValueError` with the test name, node name, resource type, demand, and capacity. If the field doesn't exist for a resource type, that resource is not validated (capacity is unknown).
6. Uses `parse_k8s_quantity()` in `common.py` to normalize both demand and capacity values to comparable numbers — handles Kubernetes quantity suffixes (`Ki`, `Mi`, `Gi`, `Ti` for binary; `n`, `u`, `m`, `k`, `M`, `G`, `T` for decimal; plain integers and floats).

For node scope, `generate_steps()` calls the validator once per (test, node) pair before calling `compute_node_steps`. For cluster scope, `compute_cluster_steps` in `cluster.py` calls the validator after placement resolution — for `setSize == 1`, once per set against the single node; for `setSize > 1`, once per DAG step against its target node (step *i* validated against node *i* of the set). Project-scoped tests skip resource validation — pods have no target nodes and run wherever the scheduler places them.

**Cluster finally steps** (`compute_teardown_steps` in `step_generator.py`) — teardown steps that run after all tests complete, regardless of success or failure. Each step has `finally_step=True`. Accepts `has_peer_steps: bool` (default `False`); when `True`, generates additional peer namespace teardown steps. All default-namespace steps carry `namespace=cs.namespace`:

1. generate `create-aggregator` — long-lived Python pod manifest
2. command `create-aggregator` — apply aggregator pod, probe: wait-ready (source: `create-aggregator`)
3. command `aggregate` — exec into aggregator pod to run `aggregate.py`
4. (if `has_peer_steps`) generate `create-peer-aggregator` — aggregator pod manifest in peer namespace, using `peer_storage` (or falling back to `cs.storage`), pod name `peer-<tc.aggregator_pod_name>`
5. (if `has_peer_steps`) command `create-peer-aggregator` — apply peer aggregator, probe: wait-ready (`namespace=cs.peer_namespace`)
6. (if `has_peer_steps`) command `peer-aggregate` — exec into peer aggregator to run `aggregate.py` (`namespace=cs.peer_namespace`)
7. command `cleanup` — delete all pods + services + configmap
8. (if `has_peer_steps`) command `peer-cleanup` — delete all pods + services + configmap in peer namespace (`namespace=cs.peer_namespace`)

### Manual Writer

`write_manual` in `writers/manual.py` writes steps to `build/manual/`. Each command step's effective namespace is `step.namespace or namespace` (the step-level namespace from computation, falling back to `cs.namespace`). This allows peer-flagged steps to target the peer namespace while default steps use the cluster namespace. Manifests go into `manual/manifests/` as data files. Shell scripts go into `manual/` with a `<counter>-` prefix indicating execution order:

1. **Ordering:** Command steps are assigned a counter in execution order, zero-padded to the width of the total step count so that shell glob ordering (`*.sh`) matches execution order. Setup steps get the initial counter values, test steps follow, and teardown steps get the final counter values. Steps that run in parallel across nodes share the same counter.

2. **Writing:** For each step:
   - **Generate steps (manifests):** content is written as `manifests/<name>.yaml` — no counter prefix. These are reference data, not actions.
   - **Command steps (apply):** a shell script is generated that applies the manifest and handles the probe. For `probe: none`: just `oc apply`. For `probe: wait-ready`: apply, then `oc wait --for=condition=Ready` with the step's timeout, then tail recent logs. For `probe: poll-completed`: apply, wait for the pod to start, stream logs in real time with `oc logs -f`, then poll for a terminal phase before checking the result (exits non-zero on failure). Written as `<counter>-<name>.sh`.
   - **Command steps (exec, delete, delete-all):** a shell script is derived from the step config. Written as `<counter>-<name>.sh`. Per-test `finally-teardown` steps are included — they give the operator a single "clean up everything for this test" script, useful when a step fails mid-test.
   - All manual scripts include echo statements so the operator can follow progress without reading the script source.
   - Step names already encode the test_id, test name, node or set index, so no additional prefixing is needed.

3. **Timestamp substitution:** `__TIMESTAMP__` is replaced with the `--run-id` value in all output.

### Runner

The manual writer emits the executable output; two drivers run it against a cluster. Both share the `Build`, `Item`, `Stage`, and `StepState` abstractions defined in `scripts/manual_runner.py`:

- **`scripts/manual_runner.py`** — an interactive terminal UI. It loads a build directory, presents its items, and lets the operator run them one at a time (or replay individual steps).
- **`scripts/auto_runner.py`** — a headless driver that runs inside the `uat-runner` pod (`setup/auto_runner.yaml`) using the pod's ServiceAccount (in-cluster config). It executes every item unattended and captures observability that the interactive runner leaves to the operator: per-step shell logs, pod logs/phase via the Kubernetes client, and — on failure — pod container states and namespace events. It writes `timesheet.csv` (one row per step) and `status.json` (machine-readable summary) under `--logs` (default `<build-dir>/logs`).

**Items and stages:** A `Build` parses `steps.json` and the numbered `manual/*.sh` scripts. Steps are grouped into `Item`s — one per test (`kind="test"`, keyed by `test_id`) plus lifecycle items (`kind="lifecycle"`) for the shared setup (configmap, builder, build) and teardown (aggregate, cleanup). Within an item, scripts that share a counter form a `Stage`: a stage's entries run concurrently (this is how node-scoped sequences fan out across nodes), and stages run in list order. Stages holding `finally_step` teardown are marked `is_finally`.

**Execution order:** `manual_runner` groups all lifecycle items ahead of the tests for its picker. For unattended execution, `auto_runner._ordered_items()` re-sorts into true run order — setup lifecycle, then tests, then post-test lifecycle (`aggregate`, `cleanup`) — so results aren't aggregated before any test has run.

**Preflight (auto_runner):** before any step runs (unless `--no-preflight`), each test namespace (project and, if configured, peer) is cleared so artifacts from an earlier run can't leak in. It deletes the suites' CRD instances first (they own pods that would otherwise respawn), then pods, services, and configmaps (keeping platform CA/trust bundles), then launches a short-lived pod that mounts the storage PVC and removes only this run's results subtree (`<base_path>/<run_id>`), leaving other runs' artifacts intact.

#### Failure Policy Handling

Each step carries the test's `on_failure` policy (`continue`, `skipTest`, or `abort`; empty for lifecycle steps). The runner enforces it as it walks an item's stages:

- **`continue`** — a failing step is recorded but does not stop anything; every remaining stage of the test still runs.
- **`skipTest` / `abort`** — a failing step *halts* the item: its remaining non-`finally` stages are skipped (their steps recorded as `SKIPPED`). `is_finally` stages (per-test teardown / finally-teardown) always run, so resources are cleaned up even after a skip.
- **lifecycle steps** (setup/teardown, no policy) — treated like a halt within the item; in `auto_runner`, a failed lifecycle item additionally stops the whole run, since later items depend on setup having succeeded.

`auto_runner._run_stage()` runs a stage's scripts as concurrent subprocesses, waits for all, and returns `(failed, halt, abort)`: `halt` is set when a failing step's policy is not `continue`; `abort` is set for an `abort` policy or a policyless (lifecycle) failure. `_run_item()` skips subsequent non-`finally` stages once `halt` is set. When an item reports `abort`, `run_all()` skips straight to cleanup: a failing test (or post-test lifecycle step) makes it skip every remaining test while still running the post-test lifecycle (`aggregate`, `cleanup`); a failing *setup* lifecycle step instead stops the run outright, since nothing downstream can run.

#### Sequences

A **sequence** is the ordered set of steps that executes one complete DAG cycle for a unit of work: deploy resources, run tests, collect results, and clean up. Every test produces one or more sequences — one per node (node-scoped), one per set (cluster-scoped), or one total (project-scoped) — and each is self-contained with its own resources, results, and cleanup. In the generated output a sequence is a run of consecutively numbered scripts sharing a `(test_id, node/set)` grouping; the runner turns each script counter into a `Stage`, so node-scoped sequences (which share counters) fan out in parallel while cluster-scoped sequences (distinct counters) run one set after another.

```
node-scoped sequence (one of N run in parallel):
  002-guidellm-wrk-4-vllm-server                          [persistent deploy]
    → 002-guidellm-wrk-4-pass-fail                         [ephemeral run]
    → 002-guidellm-wrk-4-cleanup-pass-fail                 [per-ephemeral cleanup]
    → 002-guidellm-wrk-4-sweep-short-burst                 [ephemeral run (sweep)]
    → 002-guidellm-wrk-4-cleanup-sweep-short-burst         [per-ephemeral cleanup]
    → 002-guidellm-wrk-4-sweep-sustained-load              [ephemeral run (sweep)]
    → 002-guidellm-wrk-4-cleanup-sweep-sustained-load      [per-ephemeral cleanup]
    → 002-guidellm-wrk-4-teardown                          [teardown]
    → 002-guidellm-wrk-4-finally-teardown                  [finally-teardown, always runs]

cluster-scoped sequences (setSize: 2, setSelection: all — sequential):
  0000: 003-network-0000-iperf-server                      [persistent deploy]
    → 003-network-0000-iperf-client                        [ephemeral run]
    → 003-network-0000-cleanup-iperf-client                [per-ephemeral cleanup]
    → 003-network-0000-teardown                            [teardown]
    → 003-network-0000-finally-teardown                    [finally-teardown]
  → 0001: 003-network-0001-iperf-server
    → 003-network-0001-iperf-client
    → 003-network-0001-cleanup-iperf-client
    → 003-network-0001-teardown
    → 003-network-0001-finally-teardown
  → ...

project-scoped sequence (single, no nodeSelector):
  004-quota-check-runner                                   [ephemeral run]
    → 004-quota-cleanup-check-runner                        [per-ephemeral cleanup]
    → 004-quota-finally-teardown                            [finally-teardown]
```

## Config Field Usage Map

Every parsed config field and where it takes effect. **This is the section to check when adding or auditing fields.**

### TestSuite (test suite YAML)

| Field | Model | Effect |
|---|---|---|
| `spec.tests[]` | `TestEntry` (list) | Ordered list of tests to run. List order determines execution order across all scopes |
| `spec.tests[].name` | `TestEntry.name` | Test name — resolves to `<name>.yaml` definition and `<name>.go` source |
| `spec.tests[].scope` | `TestEntry.scope` | One of `node`, `cluster`, `project`. Validated against the test definition's `metadata.supportedScopes` at load time. Determines execution pattern: node tests fan out to parallel sequences (one per node), cluster tests produce one sequence per node set with placement-controlled distribution (sequential), project tests produce a single sequence without node affinity |
| `spec.tests[].onFailure` | `TestEntry.on_failure` | Per-test failure policy (default: `continue`). `continue`: keep executing remaining steps within this test before proceeding to the next. `skipTest`: skip remaining steps in the failing sequence (tear down its resources), proceed to the next test. Other sequences are unaffected. `abort`: halt the failing test, skip every remaining test, and go straight to teardown/cleanup |
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
| `spec.nodes[].name` | `NodeSpec.name` | Node name for `nodeSelector` pinning and step name prefixing. Fit to 10 chars (`fit(node, 10)`) for the `node` field of resource names and the `node` label |
| `spec.nodes[].componentValidation.sanity.*` | `SanityCheck` (extra="allow") | Keys use actual Kubernetes resource names (e.g., `nvidia.com/gpu`, `cpu`, `memory`) for resource validation: the generator compares peak DAG step resource demands against these values. The `resourceNames` sub-dict maps resource keys to hardware model names. Non-resource fields (`nvlink`, `numaNodes`, etc.) are available for component validation checks and Jinja2 templates but are not used for resource validation |
| `spec.nodes[].componentValidation.*` | `ComponentValidation` (extra="allow") | All fields available in Jinja2 templates as `{{ nodeSpec.componentValidation.* }}` |
| `spec.compliance.*` | `ComplianceConfig` | Cluster-wide compliance settings. Consumed by Go test binaries via the embedded `cluster.yaml`, not by the harness itself |
| `spec.namespace` | `ClusterTestSpec.namespace` | Kubernetes namespace for all generated resources. Used as the default namespace for all steps; peer-flagged DAG steps use `peerNamespace` instead |
| `spec.peerNamespace` | `ClusterTestSpec.peer_namespace` | Namespace for peer-flagged DAG steps. Peer steps deploy to this namespace with independent infrastructure (ConfigMap, builder, aggregator). Defaults to `""` — only needed when a test has `peer: true` DAG steps |
| `spec.storage.pvc` | `StorageConfig.pvc` | PVC name mounted on all pods (via `subPath` — see PVC Directory Hierarchy) |
| `spec.storage.basePath` | `StorageConfig.base_path` | Root of the directory hierarchy on the PVC: `<basePath>/<timestamp>/<step_name>/`. See PVC Directory Hierarchy |
| `spec.storage.models.pvc` | `ModelsStorageConfig.pvc` | Optional PVC name for pre-downloaded model weights. When set, all DAG pods (persistent and ephemeral) get a read-only mount at `/models`. When empty or omitted, no models volume is mounted |
| `spec.peerStorage` | `ClusterTestSpec.peer_storage` | Optional `StorageConfig` for the peer namespace. When set, peer-flagged steps use this PVC and base path. When `null` (default), peer steps fall back to `spec.storage` |

### Test (`<test-lib>/<test>.yaml`)

| Field | Model | Effect |
|---|---|---|
| `metadata.supportedScopes` | `TestMetadata.supported_scopes` | List of scopes this test supports (subset of `["node", "cluster", "project"]`). Defaults to all three. `load_config()` validates the suite entry's `scope` against this list and raises `ValueError` on mismatch |
| `spec.source.ginkgo` | `TestSource` | Path (relative to test library dir) to the Ginkgo test file, read into `LoadedTest`. `go.mod` is generated at build time with the Ginkgo version from `config.yaml` |
| `spec.dag[].persistsThroughSweep` | `DAGStep.persists_through_sweep` | `true`: rendered as generate + command (apply, wait-ready) pod (+ service); stays up for all sweep entries. `false`: rendered as generate + command (apply, poll-completed) pod; one per sweep entry |
| `spec.dag[].service` | `DAGStep.service` | If `enabled: true`, generates a Service manifest and populates `{{ services["name"].url }}` in template context. `headless: true` (default) creates a headless Service (ClusterIP: None) |
| `spec.dag[].command` | `DAGStep.command` | Structured command: `args` + `flags` → `["arg1", "--key=value"]`. Flags with a `None` value (YAML `~` or empty value) render as bare flags (`--key`). Both persistent and non-persistent steps render command args through the Jinja2 template context (`serverConfig`, `nodeSpec`, `services`, `node`, `timestamp`). Non-persistent steps additionally have `paramSweep` available |
| `spec.dag[].labelFilter` | `DAGStep.label_filter` | If set, takes priority over `command`: generates a ginkgo command with `--ginkgo.label-filter=<value>` and `--ginkgo.junit-report=/uat_workspace/junit.xml`. Also auto-injects `RESULTS_DIR` env var if not already present |
| `spec.dag[].parameterSweep` | `DAGStep.parameter_sweep` | If set: one test pod per `entries[]`. Each entry's `flags` are merged over `baseCommand.flags`. If null: single test pod using the step's own command |
| `spec.dag[].env` | `DAGStep.env` | Env vars. Each entry has either a `value` (rendered through Jinja2) or a `valueFrom` (passed through as-is — supports fieldRef, secretKeyRef, configMapKeyRef) |
| `spec.dag[].resources` | `DAGStep.resources` | Resource requests/limits. Values are rendered through Jinja2 with the full template context (`nodeSpec`, `serverConfig`, `services`, `node`, `timestamp`), so expressions like `{{ nodeSpec.componentValidation.sanity["nvidia.com/gpu"] }}` work in both persistent and non-persistent steps. |
| `spec.dag[].labels` | `DAGStep.labels` | Custom labels added to pod metadata (dict of key-value strings). Rendered after fixed labels so custom labels can override them |
| `spec.dag[].sidecars` | `DAGStep.sidecars` | List of `SidecarContainer` specs. Rendered as `initContainers` with `restartPolicy: Always` (native K8s sidecar pattern). Each sidecar's `env`, `args`, `command`, and `resources` are rendered through Jinja2. Not allowed on resource steps |
| `spec.dag[].resourceConfig` | `DAGStep.resource_config` | If set, the DAG step deploys an arbitrary Kubernetes resource instead of a pod. Contains `apiVersion`, `kind`, optional `annotations` (dict, rendered onto `metadata.annotations`), and `spec` (dict). Both `annotations` and `spec` values are recursively rendered through Jinja2. Mutually exclusive with `persistsThroughSweep`, `parameterSweep`, and `sidecars`. When set, `image` is not required |
| `spec.dag[].serviceAccountName` | `DAGStep.service_account_name` | If set, the generated pod runs under this service account. Rendered as `spec.serviceAccountName` in the pod manifest |
| `spec.dag[].peer` | `DAGStep.peer` | If `true` (default `false`), the step deploys to the peer namespace. The compute functions route the step's namespace, PVC, base path, and models storage to the peer variants. Teardown is split: peer steps get their own teardown/finally-teardown pair |
| `spec.dag[].volumeMounts` | `DAGStep.volume_mounts` | Extra volume mounts added to the container. Must pair with `volumes` entries |
| `spec.dag[].volumes` | `DAGStep.volumes` | Raw volume definitions (list of dicts). Rendered as-is via `to_yaml` filter. For test pods, these are in addition to the hardcoded PVC volume |
| `spec.dag[].ports` | `DAGStep.ports` | Container ports |
| `spec.dag[].readinessProbe` | `DAGStep.readiness_probe` | Readiness probe (persistent DAG steps only) |
| `spec.dag[].privileged` | `DAGStep.privileged` | If `true`: sets `securityContext.privileged: true` and `hostPID: true` |
| `spec.serverConfig` | `TestSpec.server_config` | Dict of variables available in Jinja2 templates as `{{ serverConfig.* }}` |

### ToolConfig (`config.yaml`)

| Field | Model | Effect |
|---|---|---|
| `oseCLIImage` | `ToolConfig.ose_cli_image` | Image for steps that run `oc` commands |
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
| `defaultTestTimeout` | `ToolConfig.default_test_timeout` | Default timeout for test pod completion polling, integer seconds (default `600`). Can be overridden per-test via `timeout` in the test suite |

## Timestamp Flow (Critical Path)

The timestamp is used for results path isolation between runs. Getting it wrong means the aggregator can't find results.

```
main() computes all steps with timestamp='__TIMESTAMP__'
  │
  └── Manual output: _stamp() replaces '__TIMESTAMP__' → args.run_id (e.g. 'manual-run')
      Workspace at: /uat_workspace (subPath: <basePath>/<run-id>/<step_name>/)
```

## PVC Directory Hierarchy and Volume Mounting

Every DAG step gets a unique directory on the PVC, named after the step. The step name encodes all hierarchy information (test_id, test name, node or set index, DAG step), so directories are flat under the timestamp. Test authors do not specify paths — they write to `/uat_workspace` and files land in the right place.

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

Concrete example with `basePath=uat/results`, two node-scoped tests (component, guidellm), a cluster-scoped test, and a project-scoped test:

```
uat/results/uat-cluster-run-abc12/
  binaries/
    component/test.bin
    guidellm/test.bin
  001-component-wrk-4-test-runner/
    junit.xml
  001-component-wrk-6-test-runner/
    junit.xml
  002-guidellm-wrk-4-vllm-server/           ← persistent DAG pod workspace (logs, cache)
  002-guidellm-wrk-4-pass-fail/
    junit.xml
  002-guidellm-wrk-4-sweep-short-burst/
    junit.xml
    results.json
  002-guidellm-wrk-4-sweep-sustained-load/
    junit.xml
  002-guidellm-wrk-4-sweep-long-context/
    junit.xml
  002-guidellm-wrk-6-vllm-server/
  002-guidellm-wrk-6-pass-fail/
    junit.xml
  ...
  003-network-0000-iperf-server/            ← cluster-scoped, set 0
  003-network-0000-iperf-client/
    junit.xml
  003-network-0001-iperf-server/            ← cluster-scoped, set 1
  003-network-0001-iperf-client/
    junit.xml
  ...
  004-quota-check-runner/                   ← project-scoped (no node segment)
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
| Cluster (multiple sets) | `<basePath>/__TIMESTAMP__/<test_id>-<test>-<set>-<dag_step>` |
| Cluster (multiple sets, with sweep) | `<basePath>/__TIMESTAMP__/<test_id>-<test>-<set>-<dag_step>-<id>` |
| Cluster (single set) | `<basePath>/__TIMESTAMP__/<test_id>-<test>-<dag_step>` |
| Cluster (single set, with sweep) | `<basePath>/__TIMESTAMP__/<test_id>-<test>-<dag_step>-<id>` |
| Project | `<basePath>/__TIMESTAMP__/<test_id>-<test>-<dag_step>` |
| Project (with sweep) | `<basePath>/__TIMESTAMP__/<test_id>-<test>-<dag_step>-<id>` |

The `__TIMESTAMP__` placeholder is substituted by the manual writer with the `--run-id` value.

### Pod Volume Mounting

Each pod type mounts the PVC with a `subPath` scoped to its role. DAG pods also get a second mount at `/binaries` for access to compiled test binaries.

| Pod type | `/uat_workspace` | `/binaries` | `/models` | Notes |
|---|---|---|---|---|
| Builder | subPath: `<basePath>/<ts>/binaries` | — | — | Writes to `/uat_workspace/<test>/test.bin` |
| Aggregator | subPath: `<basePath>/<ts>` | — | — | Scans step directories for `junit.xml` |
| Persistent DAG pod | subPath: `<basePath>/<ts>/<step_name>` | subPath: `<basePath>/<ts>/binaries` | models PVC root (read-only, if configured) | Server logs, model cache written to `/uat_workspace` |
| Ephemeral test pod | subPath: `<basePath>/<ts>/<step_name>` | subPath: `<basePath>/<ts>/binaries` | models PVC root (read-only, if configured) | `junit.xml` written to `/uat_workspace` |

Because `/uat_workspace` IS the step's unique directory:
- Test pods write `junit.xml` to `/uat_workspace/junit.xml`
- Ginkgo binaries are accessed at `/binaries/<test>/test.bin`
- Benchmark tools use `output-dir: /uat_workspace`
- Pre-downloaded model weights are available at `/models/<org>/<model>` (when `storage.models.pvc` is configured)

## Pod Name Conventions

Pod, service, and resource `metadata.name` values are the step's `resource_name` — the fixed-width `build_resource_name()` output (see [Resource Naming](#resource-naming)), **not** the human-readable step name. The `test_id`, DAG step name, node, set index, and sweep id are carried in the positional fields of that name, so collisions are impossible across tests, nodes, sets, and sweeps. The table below shows which `build_resource_name()` fields each resource populates (empty fields are dash-padded):

| Resource | `type` | Populated fields |
|---|---|---|
| Persistent / test DAG pod (node) | `pod` | `test_id`, `step=<dag_step.name>`, `node` |
| Test DAG pod (node, sweep) | `pod` | `test_id`, `step=<dag_step.name>`, `node`, `sweep=<id>` |
| DAG pod (cluster, multiple sets) | `pod` | `test_id`, `step=<dag_step.name>`, `node`, `set_key` |
| DAG pod (cluster single set / project) | `pod` | `test_id`, `step=<dag_step.name>`, `node` (empty for project) |
| Service | `svc` | same fields as its owning pod, `step=<service.name>` |
| Arbitrary resource (`resourceConfig`) | `crd` | `test_id`, `step=<dag_step.name>`, `set_key` (crd form omits `node`/`sweep`) |
| Per-ephemeral cleanup | `cln` | same fields as its ephemeral pod |
| Teardown / finally-teardown | `tdn` / `ftd` | `test_id`, `node`, `set_key` |
| Builder pod | — | `<tc.builder_pod_name>` (fixed name, e.g. `ginkgo-builder`) |
| Aggregator pod | — | `<tc.aggregator_pod_name>` (fixed name, e.g. `uat-aggregator`) |

For example, the persistent DAG pod whose step name is `002-guidellm-wrk-4-vllm-server` has `metadata.name` = `ua-002-pod-vllm-server------wrk-4--------------------t`, and its Service has `metadata.name` = `ua-002-svc-vllm-server------wrk-4--------------------t`.

Services no longer use a `svc-` prefix — the `svc` type code occupies a fixed position in the name and every `build_resource_name()` output already starts with the DNS-1035-safe `ua-` prefix (services must start with a letter). Service URLs in the template context resolve to the built name: `{{ services["vllm-server"].url }}` → `http://ua-002-svc-vllm-server------wrk-4--------------------t:8000`.

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
| `services["name"]` | Service context from DAG steps with `service.enabled`. Each entry has `.url` (full URL), `.name` (Kubernetes service name), `.port` (port number) | `{{ services["vllm-server"].url }}` |
| `timestamp` | `__TIMESTAMP__` placeholder | Replaced at output time |
| `node` | Node name | `wrk-4` |
| `resource_name` | The current resource step's `build_resource_name()` value (only present for `resourceConfig` steps, from `services["_resource_name"]`) | `ua-004-crd-inferencepool---------------0000-t` |
| `namespace` | Target Kubernetes namespace | `uat-project` |


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
│   └── generate_steps(...)                                         [src/step_generator.py]
│       ├── load_tool_config(config_path)                           [src/common.py]
│       ├── load_config(suite_path, lib_dir, cluster_path)          [src/common.py]
│       │   Loads test definitions, validates scope against metadata.supportedScopes,
│       │   applies spec overrides
│       │
│       ├── compute_setup_steps(...)                                [src/step_generator.py]
│       │   Produces generate + command steps for configmap, builder pod, build
│       │   (all steps carry namespace=cs.namespace)
│       │
│       ├── for each test (by scope):
│       │   ├── compute_node_steps(...)                             [src/node.py]
│       │   │   Per node, per test: DAG deployment, test execution, teardown.
│       │   │   Accepts peer_namespace, peer_pvc, peer_base_path, peer_models_storage.
│       │   │   Routes peer-flagged steps to peer namespace; split teardown if has_peer
│       │   │
│       │   ├── compute_cluster_steps(...)                          [src/cluster.py]
│       │   │   Per set, per test: placement resolution, set generation,
│       │   │   nodeSelector assignment, DAG deployment, teardown.
│       │   │   Same peer parameters and split teardown as node scope
│       │   │
│       │   └── compute_project_steps(...)                          [src/project.py]
│       │       Single sequence per test: DAG deployment, teardown (no node affinity).
│       │       Same peer parameters and split teardown as node scope
│       │
│       ├── if any test step targets peer namespace:
│       │   Appends peer setup steps to setup_steps:
│       │   apply-peer-configmap, create-peer-builder, peer-build
│       │   (all carry namespace=cs.peer_namespace)
│       │
│       ├── compute_teardown_steps(..., has_peer_steps=bool)        [src/step_generator.py]
│       │   Produces aggregator, aggregate, cleanup steps.
│       │   If has_peer_steps: also peer-aggregator, peer-aggregate, peer-cleanup
│       │
│       ├── _validate_unique_pod_names(steps)                       [src/step_generator.py]
│       │   Validates pod names for RFC 1123 compliance and uniqueness
│       │   (scoped by namespace: uses (pod_name, step.namespace) tuples)
│       │
│       ├── _validate_service_names(steps)                          [src/step_generator.py]
│       │   Validates service names for DNS-1035 compliance
│       │
│       └── write_steps_file(...)                                   [src/step_generator.py]
│           Serializes steps to steps.json for round-tripping
│
└── write_manual(...)                                               [src/writers/manual.py]
    Generate steps → write manifests to manifests/ (no counter); command steps → derive numbered shell scripts
```

## Template File Reference

**Generate step templates** (produce content for generate steps):

| Template | Produces |
|---|---|
| `configmap.yaml.j2` | ConfigMap with all source files. Iterates over a `files` dict (filename → content), embedding each via `indent` filter. Labels with managed-by |
| `support-pod.yaml.j2` | Builder and aggregator pods. Runs `sleep infinity`, mounts PVC at `/uat_workspace` with optional `subPath`, and optionally mounts a ConfigMap at `/src`. `restartPolicy: Never` |
| `dag-pod.yaml.j2` | Persistent DAG pod manifest. Supports `nodeSelector`, `serviceAccountName`, `hostPID` + `privileged` mode, structured command with `yaml_quote`, env vars (both plain `value` and `valueFrom` references — fieldRef, secretKeyRef), ports, `readinessProbe`, resource requests/limits, extra `volumeMounts` and `volumes`, custom labels (`extra_labels`), sidecar containers (rendered as `initContainers` with `restartPolicy: Always` — native K8s sidecar pattern), and hardcoded PVC mounts at `/uat_workspace` (subPath: step dir) and `/binaries` (subPath: binaries dir). When `models_storage` is set with a non-empty `pvc`, adds a read-only mount at `/models` from the models PVC. `restartPolicy: Never` |
| `dag-service.yaml.j2` | Kubernetes Service for DAG pods. Supports headless services (`clusterIP: None`). Labels: `test`, `dag-step`, `node`, `chain`, `sweep`. Selector matches pod labels for targeted routing |
| `test-pod.yaml.j2` | Run-to-completion test pod manifest. Identical to `dag-pod.yaml.j2` except: always includes a `sweep` label (for targeted cleanup by the per-ephemeral cleanup step), and does not support `readinessProbe` (ephemeral pods are poll-completed, not wait-ready). Same conditional `/models` mount, `serviceAccountName`, `extra_labels`, sidecars, and `valueFrom` env support as `dag-pod.yaml.j2` |
| `resource.yaml.j2` | Generic manifest for arbitrary Kubernetes resources. Renders `apiVersion`, `kind`, `metadata` (name, namespace, labels including managed-by, test, optional node and chain, and optional `annotations`), and `spec` (rendered via `to_yaml`). Used by `add_resource_steps()` for resource DAG steps |

| Template | Produces |
|---|---|
| `build.sh.j2` | Build script embedded in ConfigMap. Sets Go environment (`HOME=/tmp`, `GOCACHE`, `GOPATH`), copies `cluster.yaml` to workspace, then for each test: copies `<test>_test.go`, initializes `go.mod` with `go mod init test` + `go mod edit -require ginkgo@<version>`, runs `go mod tidy` + `go mod download`, builds with `ginkgo build -r .`, and renames the output to `test.bin` |

**Manual script templates** (derived from command step config by the manual writer):

| Template | Produces |
|---|---|
| `apply-script.sh.j2` | Three code paths based on `probe`. **`none`**: `oc apply -f <manifest>`. **`wait-ready`**: apply, then `oc wait --for=condition=Ready --timeout=<N>s`, then tail 10 lines of logs. **`poll-completed`**: apply, poll until pod starts (Running/Succeeded/Failed), stream logs with `oc logs -f`, then check terminal phase — if still running after logs end, poll with a deadline (5s interval) until Succeeded/Failed/timeout |
| `exec-script.sh.j2` | `oc exec <target> -- <args \| shell_join>` |
| `teardown-script.sh.j2` | `oc delete <resource_types> -l <selector> --ignore-not-found`. Resource types default to `pods,services` but include additional types when resource steps are present |
| `cleanup-script.sh.j2` | Deletes all pods and services by managed-by label, plus the named ConfigMap |

## Pydantic Model Reference

| Model | YAML source | Key fields |
|---|---|---|
| `TestSuite` | test suite YAML | `spec.tests[]` — ordered list of `TestEntry` (name, scope, onFailure, timeout, placement, spec) |
| `Test` | `<test>.yaml` | `metadata` (`TestMetadata`), `spec.dag[]`, `spec.source`, `spec.serverConfig` |
| `TestMetadata` | nested in `Test` | `name` (str, default `""`), `supported_scopes` (alias `supportedScopes`, list of `Literal["node", "cluster", "project"]`, default `["node", "cluster", "project"]`). `model_config = ConfigDict(populate_by_name=True)` |
| `DAGStep` | nested in `Test` | `name`, `image` (optional — defaults to `""`, required for pod steps but not resource steps), `command`, `env`, `service`, `ports`, `readinessProbe`, `resources`, `volumeMounts`, `volumes`, `privileged`, `persistsThroughSweep`, `parameterSweep`, `labelFilter`, `labels` (custom pod labels dict), `sidecars` (list of `SidecarContainer`), `resourceConfig` (`ResourceConfig`, mutually exclusive with pod-step fields), `serviceAccountName`, `peer` (bool, default `False` — routes step to peer namespace). **Model validators:** (1) `persistsThroughSweep` and `parameterSweep` are mutually exclusive — setting both raises `ValueError`. (2) `_check_resource_vs_pod` — resource steps (those with `resourceConfig`) reject `persistsThroughSweep`, `parameterSweep`, and `sidecars`; pod steps (no `resourceConfig`) require a non-empty `image` |
| `SidecarContainer` | nested in `DAGStep` | `name`, `image`, `command`, `args`, `env`, `ports`, `resources`, `volumeMounts`. Rendered as init containers with `restartPolicy: Always` (native K8s sidecar pattern) |
| `ResourceConfig` | nested in `DAGStep` | `apiVersion`, `kind`, `annotations` (dict of str, default `{}`), `spec` (dict). Defines an arbitrary Kubernetes resource to deploy as part of the DAG |
| `ParameterSweep` | nested in `DAGStep` | `baseCommand.{args,flags}`, `entries[].{id,description,flags}` |
| `ClusterTest` | `cluster/*.yaml` | `spec.nodes[]`, `spec.namespace`, `spec.peerNamespace` (alias `peerNamespace`, defaults to `""`), `spec.storage.{pvc,basePath,models}`, `spec.peerStorage` (alias `peerStorage`, optional `StorageConfig`, defaults to `None`), `spec.compliance` (`ComplianceConfig`, defaults via `default_factory`). `model_config = ConfigDict(populate_by_name=True)` |
| `ComplianceConfig` | nested in `ClusterTestSpec` | `fips_enabled` (alias `fipsEnabled`, bool, default `False`). Parsed and passed through into the mounted `cluster.yaml` for Go tests; not consumed by the generator or templates |
| `ModelsStorageConfig` | nested in `StorageConfig` | `pvc` — PVC name for model weights. When non-empty, all DAG pods get a read-only mount at `/models`. Designed as a separate model so the backing store can be extended beyond PVC |
| `NodeSpec` | nested in `ClusterTest` | `name`, `componentValidation.sanity.*` (all via `extra="allow"`, keys use K8s resource names) |
| `ToolConfig` | `config.yaml` | `oseCLIImage`, `builderImage`, `ginkgoVersion`, `aggregatorImage`, `configmapName`, `builderPodName`, `aggregatorPodName`, `nodeSelectorKey`, `managedByLabel`, `builderTimeout`, `aggregatorTimeout`, `deployTimeout`, `defaultTestTimeout` |
| `LoadedTest` | (dataclass) | `name`, `spec: TestSpec`, `go_source`, `on_failure`, `timeout`, `test_id`, `scope`, `placement` |
| `Step` | (dataclass) | `name`, `type` (`generate` or `command`), `config` (type-specific: `output`/`command`/`probe`/`timeout`), `content` (generate only), `source` (command only, list of generate step names), `resource_name` (fixed-width Kubernetes `metadata.name` from `build_resource_name()`), `node` (node name, empty for global steps), `test` (test name, empty for setup/teardown), `test_id` (zero-padded 3-digit 1-indexed position in test suite, e.g. `001`, empty for setup/teardown), `on_failure` (test policy: `continue`/`skipTest`/`abort`, empty for setup/teardown), `finally_step` (if `true` and no `test`: a global teardown step that runs after all tests; if `true` and has `test`: the test's per-sequence teardown that always runs), `lifecycle` (`true` for cleanup, teardown, and finally-teardown steps — always run regardless of failure policy, excluded from the runner's per-test pass/fail check), `scope`, `phase`, `namespace` (target Kubernetes namespace; the writer resolves as `step.namespace or default_namespace`) |
| `StepsFile` | `steps.json` | `metadata` (must contain `toolConfig`, `clusterSpec`, and `setMappings` for cluster-scoped tests recording which nodes are in each set keyed by `test_id`), `steps[]` — flat list of serialized steps. **Model validator** runs `_validate_section` and `_validate_on_failure` (see StepsFile Validation Rules below) |

### StepsFile Validation Rules

When loading from `steps.json`, the `StepsFile` model validator runs two validation passes in `models.py`:

**`_validate_section(steps)`** — structural validation of each step:
1. Every step must have a non-empty `name`
2. `type` must be `"generate"` or `"command"`
3. No duplicate `(config.pod_name, namespace)` tuples across steps (uniqueness is scoped by namespace, so the same pod name in different namespaces does not collide)
4. Generate steps must have non-empty `content` and `config.output`
5. Command steps must have `config.command` in `{apply, exec, delete, delete-all}` and `config.probe` in `{none, wait-ready, poll-completed}`
6. `source` references in command steps must point to a preceding generate step's `name` (forward references are rejected)

**`_validate_on_failure(steps)`** — failure policy consistency:
1. Command steps with a `test` field must have `on_failure` in `{continue, skipTest, abort}`
2. Command steps without a `test` field (setup/teardown) must have empty `on_failure`
3. Generate steps are skipped (they carry no failure policy)

Additionally, `load_steps_file()` in `step_generator.py` validates that `metadata` contains valid `toolConfig` and `clusterSpec` by constructing `ToolConfig` and `ClusterTestSpec` from them. After loading, it runs `_validate_unique_pod_names` (RFC 1123 + uniqueness) and `_validate_service_names` (DNS-1035) on the reconstructed step list.

## Resource Validation

For node-scoped and cluster-scoped tests, `validate_node_resources` in `common.py` computes peak concurrent resource demand per target node (sum of persistent + max of ephemeral DAG step resource requests) and compares against the node's `componentValidation.sanity` fields. Resource steps (those with `resourceConfig`) are excluded — they don't produce pods and have no resource requests. Sanity dict keys use actual Kubernetes resource names (e.g. `nvidia.com/gpu`, `cpu`, `memory`) so they match resource requests directly. The generator aborts with an error if any resource type demand exceeds the node's declared capacity. This catches over-subscription (e.g., two GPU-hungry DAG steps on a 4-GPU node) at generation time. Project-scoped tests skip this check — pods have no specific target nodes.

For cluster-scoped tests, `setRequirements` in the placement config filters nodes by comparing requirement values against the sanity dict. Numeric values check `>=`, string values check exact match.

## Aggregation Script

`scripts/aggregate.py` is deployed via the ConfigMap and executed by the aggregator pod. It is a standalone Python script with no dependencies beyond the standard library.

**`parse_junit(path)`** — parses a JUnit XML file. Handles both `<testsuites>` (iterates child `<testsuite>` elements) and bare `<testsuite>` root elements. Accumulates `tests`, `failures`, `errors`, and `skipped` counts from each suite's attributes. Returns a dict of counts.

**`main()`** — CLI entry point. Takes a single argument: the results directory path. Walks the directory tree with `os.walk`, pruning `binaries/` and `report/` from `dirnames` (so they're never descended into). For each directory containing `junit.xml`, calls `parse_junit` and accumulates totals. Each directory produces a per-entry dict with its relative path as `name`, the four counts, and a `status` of `"passed"` (no failures or errors) or `"failed"`. Writes `report/summary.json` with `{"status": <overall>, "totals": {...}, "entries": [...]}` and prints the summary to stdout. Exits 1 if no arguments provided.

## Manual Writer Script Permissions

`_make_executable()` in `writers/manual.py` adds execute permissions (user, group, other) to all generated `.sh` files via `os.chmod`, so they can be run directly without `bash <script>`.

## Example Test Library Reference

See [`test_lib/README.md`](test_lib/README.md) for detailed documentation of the example test implementations.

## Dependencies and CI

**Python dependencies** (`requirements.txt`): `jinja2>=3.1`, `pydantic>=2.0`, `pytest>=7.0`, `pyyaml>=6.0`.

**CI pipeline** (`.github/workflows/ci.yaml`): two GitHub Actions jobs triggered on push/PR to `main`:
- `lint`: runs `ruff check` + `ruff format --check`
- `test`: matrix across Python 3.10–3.13, installs dependencies, runs `pytest tests/ -v`

## Known Constraints

- **ConfigMap 1MB limit:** All Go source, cluster config, test suite config, build script, and aggregator script are packed into a single ConfigMap. A project with many tests may exceed Kubernetes' 1MB ConfigMap limit.
- **Resource name width**: Kubernetes `metadata.name` values come from `build_resource_name()`, which produces a fixed-width, dash-padded, DNS-1035-valid name bounded at 54 chars (34 for the crd form) regardless of input — over-long fields are truncated and hash-suffixed by `fit()`, so names never exceed the 63-char limit. The human-readable step name (used for filenames and PVC directories) is unbounded, but it is not a Kubernetes object name.
- **Suite and set caps**: `test_id` is 3 digits and `set_key` is 4 digits, so a suite is capped at **999 tests** (`load_config` aborts above this) and a cluster-scoped test at **9999 sets** (`compute_cluster_steps` aborts above this) — the caps keep those fields within their fixed widths.
- **One run per namespace**: the builder pod has a fixed name, so only one run can execute at a time in a given namespace. This is typically sufficient — the step sequences are the element that scales with cluster size, and a single run fans out to all target nodes.
- **Sequential sweeps**: parameter sweep entries within a test run as separate pods in sequence. Failure behavior is controlled per-test via the `onFailure` field in the test suite (`continue`, `skipTest`, or `abort`). The runner applies the policy after each test: `continue` runs every step through failures; `skipTest` and `abort` halt the failing test's remaining non-`finally` stages (finally-teardown still runs), and `abort` additionally skips every remaining test and goes straight to teardown/cleanup. When running the generated scripts by hand, they are independent and the operator controls whether to proceed.
- **Combinatorial growth for cluster tests**: `setSelection: all` generates P(n, k) sets for permutations or C(n, k) for combinations, where n is the number of eligible nodes and k is `setSize`. Each set runs as a complete DAG cycle. For large clusters with `setType: permutation` and high `setSize`, the number of sets grows factorially — e.g. 10 nodes with `setSize: 3` produces 720 permutations. Use `setSelection: random` or `setType: combination` (which produces 120 for the same parameters) to bound the run count.
