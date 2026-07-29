<!-- Assisted by Claude Opus 4.6 -->
# UAT Test Harness — Architecture

## Overview

A declarative test harness that generates Kubernetes manifests from test definitions. Given a cluster configuration (target nodes, storage, namespace), the harness computes a flat, ordered list of steps, then independently derives both manually-executable manifests and Tekton pipeline manifests from that same step list. Tests are listed in execution order, each specifying a scope (**node**, **cluster**, or **project**) and a per-test failure policy.

```
                                                                     ┌→ Manual Manifests
Test Suite + Test Library + Cluster Config → python -m src → Steps ──┤                     → OpenShift Execution → Results on PVC
                                                ↑                    └→ Tekton Manifests
                                                │
                                         steps.json (optional re-entry point)
```

Each step carries its test's failure policy from `test_suite.yaml`, set during computation. After computation, the generator validates pod and service names and serializes the step list to `steps.json`. For cluster-scoped tests, `steps.json` metadata includes a `setMappings` section that records which nodes are in each set, keyed by `test_id`. The mapping is also derivable from the step list itself (each step's manifest contains the nodeSelector), but `setMappings` provides a convenient summary — especially useful for `random` selection where the chosen set is non-deterministic. This file can be fed back to the generator via `--steps` to regenerate manual and Tekton output without re-reading test definitions — useful for editing steps externally or re-running writers with different options. When loading from `steps.json`, the generator re-validates structure, pod and service names, and failure policy labels. The Tekton writer translates failure policies into `onError` values, `when` guards, and guard tasks (see [Failure Policies](#failure-policies)).

## Input Format

The generator takes three inputs: a **test suite** (`test_suite.yaml`) that defines which tests to run and in what order, a **test library** (a directory of `<test>.yaml` and `<test>.go` files) that contains the reusable test definitions, and a **cluster config** that provides the target nodes, storage, and namespace. Each node in the cluster config declares hardware characteristics under `componentValidation.sanity`. For any resource type that DAG steps request (e.g., `nvidia.com/gpu`, `memory`), the sanity section should include a field with the matching Kubernetes resource name and the node's schedulable capacity — these are used for resource validation during generation (see [Generation](#generation)). The suite/library separation allows multiple suites to reference the same library with different configurations. Adding a test to the suite requires three things:

1. **An entry in `test_suite.yaml`** — the suite-level manifest that lists tests in execution order. Each entry specifies the test name, scope (`node`, `cluster`, or `project`), what to do on failure, and an optional per-test timeout. Test definitions in the test library are scope-agnostic — the same test can appear with different scopes across entries or suites. For cluster-scoped entries, a `placement` section controls how pods are distributed across nodes. Entries can also include a `spec` section that deep-merges over the test definition's `spec` from `<test>.yaml` — any field can be overridden, including serverConfig, requirements, and individual DAG step fields (matched by step name). This allows the same test definition in the test library to be reused with different configurations across suites. Storage settings (PVC, base path) live in the cluster config. Default timeouts and tool images live in `config.yaml`.

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
         spec:                              # override test.yaml defaults
           dag:
             vllm-server:                   # match DAG step by name
               image: custom-vllm:v2        # override image

       - name: network
         scope: cluster
         onFailure: continue
         placement:                         # cluster scope only
           setType: permutation
           setSize: 2
           setSelection: all
           setRequirements:
             gpuCount: 1
           setCutoff: 0

       - name: quota
         scope: project
         onFailure: continue
   ```

   The `onFailure` field controls what happens when a step within the test fails (default: `continue`):
   - `continue` — continue executing remaining steps within this test before proceeding to the next test.
   - `skipTest` — skip remaining steps in the failing chain (tear down its resources), proceed to the next test. Other chains are unaffected.
   - `abort` — all chains complete the current test (pass or fail), then abort the entire suite.

   The optional `timeout` field overrides the `defaultTestTimeout` from `config.yaml` for this test's ephemeral pods. If omitted, the default from `config.yaml` is used.

   The optional `placement` section (cluster scope only) controls how pods are distributed across nodes. When omitted, defaults produce a single run on one random node. Fields and their defaults:
   - `setType` — `permutation` (ordered, (A,B) ≠ (B,A)) or `combination` (unordered, {A,B} = {B,A}). Default: `combination`.
   - `setSize` — number of distinct nodes per run. When `setSize > 1`, the number of DAG steps must equal the set size — DAG step *i* is placed on node *i* of the set. When `setSize == 1`, all DAG steps are placed on the same node. Default: `1`.
   - `setSelection` — `all` generates every set of `setSize` nodes and runs each as a complete DAG cycle, `random` picks a single random set. Default: `random`.
   - `setRequirements` — filters which nodes are eligible for sets. A dict of `componentValidation.sanity` field names to values. Numeric fields are treated as minimums (node value must be ≥ required), string fields as exact matches. Only nodes passing all requirements are included in set generation. Default: empty (all nodes eligible).
   - `setCutoff` — limits the number of sets that actually run. A value of `0` means no limit (all generated sets run). When `setCutoff > 0`, the effective count is `min(setCutoff, numSets)`. Ignored when `setSelection` is `random` (which already produces a single set). Default: `1`.

   The optional `spec` section deep-merges over the test definition's `spec`. For `dag` overrides, steps are referenced by name as dict keys (not a list) and only the specified fields are overridden — unmentioned fields retain their test.yaml defaults. For all other spec fields (`serverConfig`, `requirements`), the merge is recursive.

2. **`<test>.yaml`** (in the test library) — the test definition containing:
   - **DAG**: ordered resource graph (e.g. deploy a vLLM server, then run a test pod). Each vertex declares its image, command, env, ports, probes, resources, volume mounts, an optional service, and whether it persists through the parameter sweep or runs once per sweep iteration. Vertices may also specify a Ginkgo label filter (as an alternative to an explicit command), privileged mode, and extra volumes. Non-persistent steps may include a `parameterSweep` — a base command and a list of named entries, each with an `id`, `description`, and `flags` that are merged over the base command's flags. The generator produces a separate test pod for each sweep entry.
   - **Server config**: template variables substituted into DAG commands (model name, memory settings, etc.).

3. **`<test>.go`** (in the test library) — a Ginkgo test file implementing the test logic. A single compiled binary handles all parameter sweep entries — each sweep entry runs as a separate pod with per-entry command flags and workspace directory.

## Generation

The generator takes a test suite manifest (`--test-suite`), a test library directory (`--test-lib`) containing the YAML and Go files, and a cluster config (`--cluster`) as input. It uses a three-layer architecture:

1. **Step computation** — converts test definitions into a flat, ordered list of steps. Each step is either a resource to create (pod manifest, optionally bundled with a service) or an action to execute (apply a manifest, exec into a pod, delete resources). Ordering is implicit in list position. Both output layers consume the same step list.

2. **Manual writer** — writes the steps as standalone files to `build/manual/`, organized by phase (setup, test, teardown). These are the primary output: numbered `.sh` scripts in `manual/` are what the operator runs in order. Manifests (`.yaml`) are written to `manual/manifests/` as data files — each apply script references its manifest via `oc apply -f manifests/<name>.yaml`.

3. **Tekton writer** — derives Tekton Tasks and Pipelines from the same steps. Pod manifests are embedded directly in Tekton Task scripts, so `build/tekton/` is self-contained.

Every rendered manifest must be validated at generation time — invalid YAML, missing `apiVersion`, `kind`, or `metadata.name`/`metadata.generateName` must fail the generator immediately rather than producing broken manifests that only surface at `oc apply` time. Pod names are validated for RFC 1123 label compliance (lowercase alphanumeric, hyphens, etc.) and uniqueness after computation — a duplicate would cause resource collisions. Service names are validated for DNS-1035 compliance (must start with a lowercase letter, contain only lowercase alphanumeric characters and hyphens, and end with a lowercase alphanumeric character).

For node-scoped and cluster-scoped tests, the generator validates resource demands before step computation. For each target node, it computes the peak concurrent resource demand — the sum of all persistent DAG step resource requests plus the maximum single ephemeral DAG step's resource requests — and checks that each Kubernetes resource type does not exceed the node's declared capacity in `componentValidation.sanity`. Resource values in DAG steps may be Jinja2 expressions (e.g., `{{ nodeSpec.componentValidation.sanity.gpuCount }}`); these are rendered before aggregation. For cluster-scoped tests with `setSize > 1`, each DAG step targets a different node, so each node's demand is validated independently against its own capacity. The generator aborts with an error if any resource type exceeds capacity. Project-scoped tests skip resource validation since they have no specific target nodes — pods run wherever the scheduler places them.

Each step carries two names: a human-readable **step name** (used for manual script filenames, PVC directory paths, and Tekton filenames on disk) and a **resource name** (used for Kubernetes `metadata.name` on pods, services, and Tekton tasks). For node-scoped tests, the resource name substitutes a sanitized version of the node name: invalid characters (dots, underscores, etc.) are replaced with dashes, uppercase is lowercased, and names longer than 16 characters are truncated to 12 characters with a 4-character hash suffix. When the node name is short and already RFC 1123 compliant (e.g. `wrk-4`), both names are identical. For cluster-scoped and project-scoped tests, step names contain only generator-controlled segments (`set<i>`, test name, DAG step name) that are already RFC 1123 compliant, so step name and resource name are always identical.

### Failure Policies

Each test declares an `onFailure` policy in `test_suite.yaml`. The generator labels each step with its test's failure policy. Writers are responsible for translating these policies into backend-specific mechanisms.

- **`continue`** — continue executing remaining steps within this test before proceeding to the next test. A failing step does not affect other steps or other chains (nodes, sets).
- **`skipTest`** — skip remaining steps within this test in the failing chain (tear down its resources), then proceed to the next test. Other chains running the same test are unaffected.
- **`abort`** — all chains complete the current test (pass or fail), then abort the entire suite. No further tests run.

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
    ├── cluster-pipeline.yaml                    (single flat pipeline)
    ├── task-*.yaml                              (one per command step, plus one guard task per test)
    └── pipelinerun.yaml
```

Manifests (`.yaml`) are written to `manual/manifests/` without a counter prefix — they are data files, not actions. Numbered shell scripts (`.sh`) are written to `manual/` and are what the operator runs in order: apply scripts reference the corresponding manifest (`oc apply -f manifests/<name>.yaml`), exec scripts run commands, and delete scripts clean up resources. Steps that run in parallel across nodes share the same counter. The counter is zero-padded to the width of the total step count so that shell glob ordering (`*.sh`) matches execution order. The numbered scripts are the single source of "what to do, in what order."

`<test_id>` is the 1-indexed position of the test in the `test_suite.yaml` list (not zero-padded). The same test can appear multiple times in the list (e.g. with different configs or failure policies), so `<test_id>` prevents collisions in resource names and results paths, while `<test_name>` provides readability. For node-scoped tests, `<node>` is added to prevent collisions across parallel nodes. For cluster-scoped multi-set tests, `set<i>` prevents collisions across sequential sets. Project-scoped and cluster-scoped single-set tests have no node or set segment. Service names are prefixed with `svc-` for DNS-1035 compliance (services require names starting with a letter). Service URL references in env vars and commands are automatically rewritten to match.

## Execution

### Tekton Writer

The Tekton writer produces a single flat Tekton Pipeline. All tasks — setup, test, and teardown — are entries in one cluster pipeline. Node-scoped tests produce one task chain per node, running in parallel. Failure policies are implemented through `onError`, `when` expressions, and guard tasks. The cluster pipeline sequences tests in `test_suite.yaml` list order. Requires `scope-when-expressions-to-task: true` (default since Tekton Pipelines v0.54).

#### Cluster Pipeline

```
apply-configmap → create-builder → build → [test task chains] → finally: create-aggregator → aggregate → cleanup
                                                                          (sequenced via runAfter)
```

**1. Apply ConfigMap** — creates a ConfigMap containing all Go source, cluster config, test suite config, build script, and aggregator script.

**2. Create builder pod** — a long-lived Go toolchain pod with the PVC mounted at `/workspace` and the ConfigMap mounted at `/src/`.

**3. Build binaries** — copies source from ConfigMap mounts into the PVC, generates a `go.mod` with the Ginkgo version pinned in `config.yaml`, and compiles one Ginkgo binary per unique test name at `/workspace/<test>/test.bin`. If the same test name appears multiple times in `test_suite.yaml` (e.g. with different failure policies), all instances share the same binary.

**4. Tests** — each test's tasks are placed directly in the cluster pipeline as individual `taskRef` entries. Scope determines the shape:

- **Node** tests produce one task chain per target node, all running in parallel (no `runAfter` between nodes for the same test). Within each chain, tasks are sequential via `runAfter`. The `finally-teardown` is the last task in each chain with no `when` guard — it always runs regardless of earlier failures. The guard task fans in after all node chains complete. Pods are pinned to the target node via `nodeSelector` in the pod manifests.

  ```
  wrk-6: A₆ → B₆ → C₆ → teardown₆ ─┐
                                       ├─→ guard-test-N → Next test
  wrk-4: A₄ → B₄ → C₄ → teardown₄ ─┘
  ```
- **Cluster** tests orchestrate tasks across nodes. Placement is controlled by the suite entry's `placement` section: `setType` (`permutation` or `combination`), `setSize` (how many distinct nodes per run), `setSelection` (which node sets to run), `setRequirements` (filters the node list by `componentValidation.sanity` fields), and `setCutoff` (limits the number of sets; ignored when `setSelection` is `random`). When `setSelection: all`, the test runs once per node set — each set is a self-contained DAG cycle (deploy, test, cleanup, teardown, finally-teardown) running sequentially. When `setSelection: random`, a single random set is chosen. For `setSize > 1`, DAG step *i* gets a `nodeSelector` pinning it to node *i* of the set. For `setSize == 1`, all DAG steps share the same node. Each set has its own `finally-teardown` — sets are treated as independent test runs. Multi-set runs include a `set<i>` segment in step names to avoid collisions, and a `chain` label on all pods and services (e.g., `chain=set0`) so that cleanup selectors scope teardown to the current set — preventing leakage between sets if a teardown fails. Single-set runs omit both the set segment and the `chain` label. The guard task fans in after the last set's `finally-teardown`.

  ```
  setType: permutation, setSize: 2, setSelection: all, 3 nodes (6 sets, sequential):
    set0 (A→B): server₀ → client₀ → cleanup₀ → teardown₀ → finally-teardown₀
      → set1 (A→C): server₁ → client₁ → cleanup₁ → teardown₁ → finally-teardown₁
      → ... → guard-test-N → Next test

  setType: combination, setSize: 2, setSelection: all, 3 nodes (3 sets, sequential):
    set0 {A,B}: server₀ → client₀ → cleanup₀ → teardown₀ → finally-teardown₀
      → set1 {A,C}: server₁ → client₁ → cleanup₁ → teardown₁ → finally-teardown₁
      → set2 {B,C}: server₂ → client₂ → cleanup₂ → teardown₂ → finally-teardown₂
      → guard-test-N → Next test

  setSelection: random, setSize: 2 (single set, no set prefix):
    server → client → cleanup → teardown → finally-teardown → guard-test-N → Next test
  ```
- **Project** tests produce a single task chain directly in the cluster pipeline, without node affinity. Pods run without `nodeSelector`, validating project-wide concerns (quotas, RBAC, network policies). Step names follow the cluster/project convention: `<test_id>-<test>-<dag_step>`.

  ```
  4-quota-runner → 4-quota-cleanup-runner → 4-quota-finally-teardown → guard-test-N → Next test
  ```

Every test, regardless of scope, ends with a guard task. The guard task fans in after all the test's `finally-teardown` tasks and serves as the single sync point between tests — the next test's first tasks `runAfter` the guard task. The guard task's `onError` is set according to the test's failure policy (see [Failure Policy Handling](#failure-policy-handling)).

Because each test's tasks are flat entries in the cluster pipeline, scopes can be freely interleaved (e.g. node test → cluster test → node test) without any grouping constraints.

All tasks reference the pipeline run name directly via `$(context.pipelineRun.name)`.

#### Failure Policy Handling

Each test declares an `onFailure` policy (`continue`, `skipTest`, `abort`). The Tekton writer translates these into `onError` values, `when` guards on individual tasks, and a guard task after each test.

**Guard tasks:** Every test gets a guard task that fans in after all chains' `finally-teardown` tasks (one chain per node for node-scoped tests, one per set for cluster-scoped, or a single chain for project-scoped). The guard task is the single sync point between tests — the next test's first tasks `runAfter` the guard task. The guard task receives all non-lifecycle task statuses as a comma-separated parameter and exits non-zero if any value is `Failed`. The `onError` on the guard task determines the consequence:

- `continue` or `skipTest` → `onError: continue` (pipeline proceeds to the next test regardless)
- `abort` → `onError: stopAndFail` (pipeline halts and jumps to cluster `finally`)

**`onError` assignment:** The Tekton writer assigns `onError` to each task based on its role:

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

- **`skipTest`** — `when` guards on non-first test steps (persistent deploys and ephemeral runs) within each chain. If a test step fails, remaining guarded test steps in that chain are skipped. Lifecycle steps — per-ephemeral cleanup, teardown, and finally-teardown — have no `when` guard and always run. Other chains are unaffected. Guard task with `onError: continue` — pipeline always proceeds to the next test.

- **`abort`** — `when` guards on non-first test steps (same as `skipTest`). Other chains complete the test normally. Guard task with `onError: stopAndFail` — if any step failed in any chain, the pipeline halts and jumps to cluster `finally`. No further tests run.

```
continue policy (2 nodes):
  wrk-6: A₆ → B₆ → C₆ → teardown₆ ─┐
                                       ├─→ guard-test-N (continue) → Next test
  wrk-4: A₄ → B₄ → C₄ → teardown₄ ─┘
  (no when guards · all tasks run regardless)

skipTest policy (2 nodes):
  wrk-6: A₆ → B₆ → C₆ → teardown₆ ─┐
                                       ├─→ guard-test-N (continue) → Next test
  wrk-4: A₄ → B₄ → C₄ → teardown₄ ─┘
  (B, C: when-guarded · teardown: always runs)

abort policy (2 nodes):
  wrk-6: A₆ → B₆ → C₆ → teardown₆ ─┐
                                       ├─→ guard-test-N (stopAndFail) → Next test
  wrk-4: A₄ → B₄ → C₄ → teardown₄ ─┘
  (B, C: when-guarded · teardown: always runs)
```

Each `when` guard checks `$(tasks.<predecessor>.status) in ["Succeeded"]`. When a task is skipped by its guard, its status becomes `None`, causing downstream guarded tasks in the same chain to also skip. The per-chain `finally-teardown` has no `when` guard, so it runs regardless — `scope-when-expressions-to-task` prevents the skip from cascading past unguarded tasks.

**5. Aggregate results (finally)** — creates an aggregator pod, then execs into it to read individual JUnit/JSON reports and generate a consolidated report. Runs after all tests complete (success or failure). Must complete before cleanup.

**6. Cleanup (finally)** — deletes all pods, services, and deployments matching the managed-by label, and the ConfigMap. Ordered after aggregation within the finally block (Tekton finally tasks run in parallel by default, so explicit ordering is required).

#### Test Task Chains

A **task chain** is a linear sequence of steps that executes one complete DAG cycle: deploy resources, run tests, collect results, and clean up. Chains are the fundamental unit of execution — every test produces one or more chains, and every chain is self-contained with its own resources, results, and cleanup.

**Chain multiplicity and execution model:**

| Scope | Chains per test | Execution | Node affinity |
|---|---|---|---|
| Node | One per target node | Parallel — all chains run concurrently | Each chain's pods pinned to one node via `nodeSelector` |
| Cluster | One per node set | Sequential — each set completes fully before the next begins | DAG step *i* pinned to node *i* of the set via `nodeSelector` |
| Project | One | Single chain | No `nodeSelector` — pods run wherever the scheduler places them |

For node-scoped tests, the number of chains equals the number of target nodes. For cluster-scoped tests, the number of chains depends on placement config: `setSelection: all` produces one chain per generated set (P(n,k) for permutations, C(n,k) for combinations, clamped by `setCutoff`); `setSelection: random` always produces one chain. For project-scoped tests, there is always exactly one chain.

**Chain lifecycle:**

Every chain follows the same five-phase lifecycle regardless of scope. DAG steps are processed in their definition order, and persistent and ephemeral steps can be interleaved freely. Phases 3–5 are **lifecycle steps** — they are generated automatically, carry `lifecycle: true` metadata, and receive no `when` guards under any failure policy:

1. **Persistent deploy** — For each DAG step with `persistsThroughSweep: true`: create the pod (and optional Service), wait for readiness. The resource stays up for all subsequent steps in this chain to use. Multiple persistent steps are deployed in order as they appear in the DAG.

2. **Ephemeral run** — For each DAG step with `persistsThroughSweep: false` (the default): apply a test pod (and optional Service), wait for completion. If the step has a `parameterSweep`, one pod is created per sweep entry, run sequentially. Results write to the PVC. Ephemeral steps can reference persistent resources (e.g. a test client hitting a persistent server).

3. **Per-ephemeral cleanup** — Immediately after each ephemeral pod completes (success or failure), a cleanup step deletes that pod and its service by label. This releases resources like GPUs for subsequent steps without affecting persistent resources. Cleanup steps are paired 1:1 with ephemeral steps.

4. **Teardown** — After all DAG steps complete, a teardown step removes all persistent resources for this chain (pods, services, deployments matching the chain's labels).

5. **Finally-teardown** — The last step in the chain, with no `when` guard. A safety net that catches anything teardown missed or anything left behind when earlier steps were skipped by failure policy. Deletes all resources — both persistent and ephemeral — matching the chain's labels. Runs unconditionally: in Tekton, `scope-when-expressions-to-task` ensures a skipped predecessor does not cascade past an unguarded task.

Lifecycle steps — per-ephemeral cleanup, teardown, and finally-teardown — have no `when` guard and run unconditionally regardless of failure policy. Only test steps (persistent deploys and ephemeral runs) receive `when` guards under `skipTest`/`abort`. Teardown is only added when the chain has persistent resources; by the time it runs, per-ephemeral cleanup has already handled ephemeral resources. Finally-teardown is always present — both use the same broad selector, but finally-teardown catches anything missed by earlier steps or left behind when steps were skipped by failure policy. For cluster-scoped tests, this guarantees a clean slate between sets.

**Chain fan-in and test sequencing:**

After all chains for a test complete, they fan in to a single **guard task**. The guard task is the sync point between tests — the next test's first steps `runAfter` the guard task. The guard task's `onError` enforces the test's failure policy (see [Failure Policy Handling](#failure-policy-handling)).

```
node-scoped (parallel chains → guard):
  chain(wrk-6): ... → finally-teardown₆ ─┐
                                           ├─→ guard-test-N → next test
  chain(wrk-4): ... → finally-teardown₄ ─┘

cluster-scoped (sequential chains → guard):
  chain(set0): ... → finally-teardown₀
    → chain(set1): ... → finally-teardown₁
    → ... → guard-test-N → next test

project-scoped (single chain → guard):
  chain: ... → finally-teardown → guard-test-N → next test
```

**Writer transparency:**

All scopes produce the same `Step` format. Placement is fully resolved during step computation — nodeSelectors, node labels, and set indices are baked into the rendered manifest content. Writers use step metadata (scope, chain keys) to determine execution ordering.

**Chain structure examples:**

```
node-scoped chain (one of N parallel chains):
  2-inference-wrk-4-vllm-server                          [persistent deploy]
    → 2-inference-wrk-4-pass-fail                         [ephemeral run]
    → 2-inference-wrk-4-cleanup-pass-fail                 [per-ephemeral cleanup]
    → 2-inference-wrk-4-sweep-short-burst                 [ephemeral run (sweep entry)]
    → 2-inference-wrk-4-cleanup-sweep-short-burst         [per-ephemeral cleanup]
    → 2-inference-wrk-4-sweep-sustained-load              [ephemeral run (sweep entry)]
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
├── 3-network-set0-iperf-server/                # cluster-scoped, set 0
├── 3-network-set0-iperf-client/
│   └── junit.xml
├── 3-network-set1-iperf-server/                # cluster-scoped, set 1
├── 3-network-set1-iperf-client/
│   └── junit.xml
├── ...
├── 4-quota-check-runner/                       # project-scoped (no node segment)
│   └── junit.xml
└── report/
    └── summary.json
```

The base path is a cluster-level setting that scopes results to a particular test suite or environment (e.g. `uat/results`). The pipeline run name provides timestamp-based isolation between runs. Each step gets a flat directory named after its step name, which encodes the test index, test name, node (for node-scoped tests) or set index (for cluster-scoped multi-set tests), and DAG step for uniqueness and readability. Test pods write to `/workspace` and files land in the right place via Kubernetes `subPath` mounting. The aggregator scans for `junit.xml` files across all step directories and writes a consolidated summary to `report/`.

## Design Decisions

| Decision | Rationale |
|---|---|
| Steps-first generation | The generator computes a flat, ordered step list from test definitions, then both the manual writer and the Tekton writer independently derive their output from that same list. This ensures both paths always produce equivalent resources, and makes it straightforward to add writers for other orchestration harnesses without changing step computation. |
| Three test scopes, one list | **Node** tests validate per-node hardware (GPUs, drivers). **Cluster** tests validate multi-node coordination (RDMA, interconnect) with configurable placement at the suite level. **Project** tests validate namespace-level concerns (quotas, RBAC) without node affinity. Test definitions are scope-agnostic — the same test can appear with different scopes across suites. All three scopes are declared in a single ordered list in `test_suite.yaml`, allowing interleaved execution — each test is its own pipeline entry in the cluster pipeline, so scopes can alternate freely. |
| Unified step naming | DAG steps follow a single naming convention: `<test_id>-<test>-<node>-<dag_step>` (node-scoped), `<test_id>-<test>-set<i>-<dag_step>` (cluster-scoped, multiple sets), or `<test_id>-<test>-<dag_step>` (cluster-scoped single set, or project-scoped), with `-<id>` appended for sweep entries. Each step carries a human-readable **step name** (used for filenames and PVC paths) and a **resource name** (used for Kubernetes `metadata.name` on pods, services, and Tekton tasks). For node-scoped tests, the resource name uses a sanitized node name where invalid characters are replaced with dashes and names over 16 characters are truncated to 12 + a 4-character hash. When the node name is short and RFC 1123 compliant, both names are identical. Cluster-scoped and project-scoped step names use only generator-controlled segments, so both names are always identical. Lifecycle steps extend the convention with a fixed suffix: `<prefix>-cleanup-<dag_step>[-<id>]` (per-ephemeral-step cleanup), `<prefix>-teardown` (persistent resource teardown), and `<prefix>-finally-teardown` (always-run safety net). `<test_id>` prevents collisions when the same test appears multiple times in the suite; `<node>` prevents collisions across parallel nodes; `set<i>` prevents collisions across node sets. Service names are prefixed with `svc-` for DNS-1035 compliance. Service URL references are rewritten automatically. |
| Placement is step computation, not writer logic | All scopes resolve placement during step computation — nodeSelectors and labels are baked into the rendered manifest content. The resulting step list uses the same `Step` format across all scopes. Writers use step metadata (scope, chain keys) to determine execution ordering. |
| One binary per test, not per parameter | Same test logic, different runtime config. Avoids redundant compilation. |
| ConfigMap → Builder Pod → PVC | A single ConfigMap delivers all Go source to the builder pod. Builder pod provides a persistent compilation environment. PVC makes binaries accessible to any test container. Delivery mechanism is swappable (GitHub pull, custom image) without changing the rest of the pipeline. |
| DAG resources persist through sweep | Expensive resources (GPU-backed servers) deploy once; the parameter sweep reuses them. |
| One Tekton task per DAG step | Each non-persistent step gets its own task (not one per test). Sweep iterations each get a separate test pod and task, keeping the Tekton task graph explicit. |
| Resource validation at generation time | Before step computation, the generator validates that each target node has sufficient resources for the test's peak concurrent demand (sum of persistent + max ephemeral). Uses `componentValidation.sanity` fields as the capacity source — any field whose name matches a Kubernetes resource type (e.g., `nvidia.com/gpu`) is compared against the rendered DAG step resource requests. Catches over-subscription at generation time rather than producing manifests that fail to schedule. |
| Ephemeral pod cleanup after each step | Non-persistent pods are deleted immediately after completion to release resources (e.g. GPUs) for subsequent steps. Each ephemeral step's pod and service carry a `sweep` label for targeted deletion without affecting persistent resources. |
| One task chain per unit of work | Each test produces one or more task chains placed directly in the cluster pipeline: one per node (node-scoped), one per set (cluster-scoped), or one total (project-scoped). Node-scoped chains run in parallel; cluster-scoped chains run sequentially (each set completes before the next begins). Different tests always run in sequence. A single Pipeline contains all tasks. |
| Per-test failure policy | Each test declares its own `onFailure` (`continue`, `skipTest`, `abort`) instead of a single global flag. The flat pipeline implements these through two mechanisms: **`when` guards** on test tasks within each chain (skip remaining test tasks after a failure), and a **guard task** after every test (fan in all results and enforce the failure policy). The guard task's `onError` is the single point where the policy takes effect: `continue`/`skipTest` → `onError: continue`, `abort` → `onError: stopAndFail`. `continue`: no `when` guards — every task runs regardless. `skipTest`: `when` guards skip remaining test tasks in the failing chain; other chains and subsequent tests are unaffected. `abort`: same within-chain behavior as `skipTest`, but the guard task halts the pipeline if any chain had a failure. The per-chain `finally-teardown` task has no `when` guard and always runs, cleaning up resources even when earlier tasks are skipped. Requires `scope-when-expressions-to-task: true` (default since Tekton v0.54). |

## Constraints

- **ConfigMap 1MB limit**: all Go source, cluster config, test suite config, build script, and aggregator script are packed into a single ConfigMap. A project with many tests may exceed Kubernetes' 1MB ConfigMap limit.
- **Resource name length**: resource names are constructed by concatenating test_id, test name, node or set segment, and DAG step (e.g. `2-inference-wrk-4-vllm-server`, `3-network-set0-iperf-server`). Node names are capped at 16 characters (12 + 4-char hash if longer), but the full resource name can still exceed the 63-character Kubernetes name limit with long test or DAG step names.
- **One cluster pipeline per namespace**: the builder pod has a fixed name, so only one cluster pipeline can run at a time in a given namespace. This is typically sufficient — the task chains are the element that scales with cluster size, and a single cluster pipeline fans out to all target nodes in parallel.
- **Sequential sweeps**: parameter sweep entries within a test run as separate pods in sequence. Failure behavior is controlled per-test via the `onFailure` field in `test_suite.yaml` (`continue`, `skipTest`, or `abort`). All three policies produce a guard task between tests. `continue` uses no `when` guards, so all tasks run through failures. `skipTest` adds `when` guards that skip remaining test tasks in the chain after a failure. Both use `onError: continue` on the guard task, so the next test always proceeds. `abort` uses the same `when` guards as `skipTest`, but the guard task uses `onError: stopAndFail` — halting the pipeline if any chain had a failure. In manual mode, scripts are independent and the operator controls whether to proceed.
- **Combinatorial growth for cluster tests**: `setSelection: all` generates P(n, k) sets for permutations or C(n, k) for combinations, where n is the number of cluster nodes and k is `setSize`. Each set runs as a complete DAG cycle. For large clusters with `setType: permutation` and high `setSize`, the number of sets grows factorially — e.g. 10 nodes with `setSize: 3` produces 720 permutations. Use `setSelection: random` or `setType: combination` (which produces 120 for the same parameters) to bound the run count.
