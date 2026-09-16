<!-- Assisted by Claude Opus -->
# UAT Test Harness — Architecture

## Overview

A declarative test harness that generates Kubernetes manifests from test definitions. Given a cluster configuration (target nodes, storage, namespace, and optional peer namespace for cross-namespace tests), the harness computes a flat, ordered list of steps, then derives manually-executable manifests and numbered shell scripts from that step list. Tests are listed in execution order, each specifying a scope (**node**, **cluster**, or **project**) and a per-test failure policy. Each test definition declares which scopes it supports; scope mismatches are rejected at generation time.

```
Test Suite + Test Library + Cluster Config → python -m src → Steps → Manual Manifests → OpenShift Execution → Results on PVC
                                                ↑
                                         steps.json (optional re-entry point)
```

Each step carries its test's failure policy from the test suite, set during computation. After computation, the generator validates pod and service names and serializes the step list to `steps.json`. For cluster-scoped tests, `steps.json` metadata includes a `setMappings` section that records which nodes are in each set, keyed by `test_id`. The mapping is also derivable from the step list itself (each step's manifest contains the nodeSelector), but `setMappings` provides a convenient summary — especially useful for `random` selection where the chosen set is non-deterministic. This file can be fed back to the generator via `--steps` to regenerate output without re-reading test definitions — useful for editing steps externally or re-running the writer with different options. When loading from `steps.json`, the generator re-validates structure, pod and service names, and failure policy labels. The runner applies each step's failure policy at execution time (see [Failure Policies](#failure-policies)).

## Input Format

The generator takes three inputs: a **test suite** that defines which tests to run and in what order, a **test library** (a directory of `<test>.yaml` and `<test>.go` files) that contains the reusable test definitions, and a **cluster config** that provides the target nodes, storage, namespace, and optional peer namespace. Each node in the cluster config declares hardware characteristics under `componentValidation.sanity`. For any resource type that DAG steps request (e.g., `nvidia.com/gpu`, `memory`), the sanity section should include a field with the matching Kubernetes resource name and the node's schedulable capacity — these are used for resource validation during generation (see [Generation](#generation)). The suite/library separation allows multiple suites to reference the same library with different configurations. Adding a test to the suite requires three things:

1. **An entry in the test suite** — the suite-level manifest that lists tests in execution order. Each entry specifies the test name, scope (`node`, `cluster`, or `project`), what to do on failure, and an optional per-test timeout. Each test definition declares which scopes it supports; the same test can appear with different (supported) scopes across entries or suites. For cluster-scoped entries, a `placement` section controls how pods are distributed across nodes. Entries can also include a `spec` section that deep-merges over the test definition's `spec` from `<test>.yaml` — any field can be overridden, including serverConfig and individual DAG step fields (matched by step name). This allows the same test definition in the test library to be reused with different configurations across suites. Storage settings (PVC, base path, optional models storage) live in the cluster config. The cluster config may also declare a `compliance` section (e.g. `fipsEnabled`); the harness does not act on it during generation, but it is serialized into the mounted `cluster.yaml` so the Go test binaries can read it. Default timeouts and tool images live in `config.yaml`.

   ```yaml
   spec:
     tests:
       - name: component
         scope: node
         onFailure: continue

       - name: guidellm
         scope: node
         onFailure: abort
         timeout: 1200
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
             nvidia.com/gpu: 1
           setCutoff: 0

       - name: quota
         scope: project
         onFailure: continue
   ```

   The `onFailure` field controls what happens when a step within the test fails (default: `continue`):
   - `continue` — continue executing remaining steps within this test before proceeding to the next test.
   - `skipTest` — skip remaining steps in the failing sequence (tear down its resources), proceed to the next test. Other sequences are unaffected.
   - `abort` — all sequences complete the current test (pass or fail), then the runner skips every remaining test and goes straight to teardown/cleanup.

   The optional `timeout` field overrides the `defaultTestTimeout` from `config.yaml` for this test's ephemeral pods. If omitted, the default from `config.yaml` is used.

   The optional `placement` section (cluster scope only) controls how pods are distributed across nodes. When omitted, defaults produce a single run on one random node. Fields and their defaults:
   - `setType` — `permutation` (ordered, (A,B) ≠ (B,A)) or `combination` (unordered, {A,B} = {B,A}). Default: `combination`.
   - `setSize` — number of distinct nodes per run. When `setSize > 1`, the number of DAG steps must equal the set size — DAG step *i* is placed on node *i* of the set. When `setSize == 1`, all DAG steps are placed on the same node. Default: `1`.
   - `setSelection` — `all` generates every set of `setSize` nodes and runs each as a complete DAG cycle, `random` picks a single random set. Default: `random`.
   - `setRequirements` — filters which nodes are eligible for sets. A dict of `componentValidation.sanity` field names to values. Numeric fields are treated as minimums (node value must be ≥ required), string fields as exact matches. Only nodes passing all requirements are included in set generation. Default: empty (all nodes eligible).
   - `setCutoff` — limits the number of sets that actually run. A value of `0` means no limit (all generated sets run). When `setCutoff > 0`, the effective count is `min(setCutoff, numSets)`. Ignored when `setSelection` is `random` (which already produces a single set). Default: `1`.

   The optional `spec` section deep-merges over the test definition's `spec`. For `dag` overrides, steps are referenced by name as dict keys (not a list) and only the specified fields are overridden — unmentioned fields retain their test.yaml defaults. For all other spec fields (`serverConfig`), the merge is recursive.

2. **`<test>.yaml`** (in the test library) — the test definition containing:
   - **Metadata**: declares which scopes the test supports (e.g. `[node, cluster]`). The generator rejects suite entries whose scope is not in this list. Defaults to all three scopes when omitted.
   - **DAG**: ordered resource graph (e.g. deploy a vLLM server, then run a test pod). DAG steps come in two flavors: **pod steps** and **resource steps**. Pod steps declare an image, command, env, ports, probes, resources, volume mounts, an optional service, and whether the pod persists through the parameter sweep or runs once per sweep iteration. `persistsThroughSweep` and `parameterSweep` are mutually exclusive — a persistent step cannot have its own sweep (it stays up while ephemeral sweep pods run against it). Pod steps may also specify a Ginkgo label filter (as an alternative to an explicit command), privileged mode, extra volumes, custom labels, a `serviceAccountName`, and sidecar containers (rendered as native Kubernetes sidecar init containers with `restartPolicy: Always`). Non-persistent pod steps may include a `parameterSweep` — a base command and a list of named entries, each with an `id`, `description`, and `flags` that are merged over the base command's flags. The generator produces a separate test pod for each sweep entry. **Resource steps** declare a `resourceConfig` (with `apiVersion`, `kind`, `spec`, and optional `annotations`) instead of an image, and deploy an arbitrary Kubernetes resource (e.g. an InferencePool or ConfigMap) as part of the DAG. Resource steps cannot set `persistsThroughSweep`, `parameterSweep`, or `sidecars`. Env vars support both plain `value` fields and Kubernetes `valueFrom` references (fieldRef, secretKeyRef). Any DAG step (pod or resource) can set `peer: true` to deploy in the peer namespace instead of the primary namespace; see [Peer Namespace](#peer-namespace).
   - **Server config**: template variables substituted into DAG commands (model name, memory settings, etc.).

3. **`<test>.go`** (in the test library) — a Ginkgo test file implementing the test logic. A single compiled binary handles all parameter sweep entries — each sweep entry runs as a separate pod with per-entry command flags and workspace directory.

## Generation

The generator takes a test suite manifest (`--test-suite`), a test library directory (`--test-lib`) containing the YAML and Go files, and a cluster config (`--cluster`) as input. It uses a three-layer architecture:

1. **Step computation** — converts test definitions into a flat, ordered list of steps. Each step is either a resource to create (pod manifest, optionally bundled with a service, or an arbitrary Kubernetes resource manifest) or an action to execute (apply a manifest, exec into a pod, delete resources). Ordering is implicit in list position. The writer consumes this step list.

2. **Manual writer** — writes the steps as standalone files to `build/manual/`, organized by phase (setup, test, teardown). These are the output: numbered `.sh` scripts in `manual/` are what the operator (or the runner) executes in order. Manifests (`.yaml`) are written to `manual/manifests/` as data files — each apply script references its manifest via `oc apply -f manifests/<name>.yaml`.

Step computation is independent of the writer, so adding a new execution backend means writing a new writer — step computation doesn't change.

Every rendered manifest must be validated at generation time — invalid YAML, missing `apiVersion`, `kind`, or `metadata.name`/`metadata.generateName` must fail the generator immediately rather than producing broken manifests that only surface at `oc apply` time. Pod names are validated for RFC 1123 label compliance (lowercase alphanumeric, hyphens, etc.) and uniqueness within each namespace after computation — a duplicate in the same namespace would cause resource collisions. Service names are validated for DNS-1035 compliance (must start with a lowercase letter, contain only lowercase alphanumeric characters and hyphens, and end with a lowercase alphanumeric character).

For node-scoped and cluster-scoped tests, the generator validates resource demands before step computation. For each target node, it computes the peak concurrent resource demand — the sum of all persistent DAG step resource requests plus the maximum single ephemeral DAG step's resource requests — and checks that each Kubernetes resource type does not exceed the node's declared capacity in `componentValidation.sanity`. Resource values in DAG steps may be Jinja2 expressions (e.g., `{{ nodeSpec.componentValidation.sanity["nvidia.com/gpu"] }}`); these are rendered before aggregation. Sanity dict keys use actual Kubernetes resource names (e.g. `nvidia.com/gpu`, `cpu`, `memory`) so they match resource requests directly. For cluster-scoped tests with `setSize > 1`, each DAG step targets a different node, so each node's demand is validated independently against its own capacity. The generator aborts with an error if any resource type exceeds capacity. Project-scoped tests skip resource validation since they have no specific target nodes — pods run wherever the scheduler places them.

Each step carries two names: a human-readable **step name** and a Kubernetes **resource name**.

The **step name** is used for manual script filenames and PVC results directories. It concatenates generator-controlled segments: `<test_id>-<test>-<node>-<dag_step>` (node-scoped), `<test_id>-<test>-<set>-<dag_step>` (cluster-scoped, multiple sets), or `<test_id>-<test>-<dag_step>` (cluster single-set or project-scoped). `<test_id>` is a zero-padded 3-digit index (`001`, `002`, …); `<set>` is a zero-padded 4-digit set index (`0000`, `0001`, …).

The **resource name** is used for Kubernetes `metadata.name` on pods, services, and resource-step objects. It is generated by `build_resource_name()` as a fixed-width positional string that is guaranteed RFC 1123 / DNS-1035 compliant and length-bounded:

```
ua-<test_id>-<type>-<step>-<node>-<set>-<sweep>-t     (pods, services, cleanup, teardown)
ua-<test_id>-<type>-<step>-<set>-t                    (resource steps / CRDs — no node or sweep)
```

Each field is right-padded with `-` to a fixed width — test_id: 3, type: 3, step: 16, node: 10, set: 4, sweep: 8 — so the name is always 54 characters (34 for the CRD form), always starts with a letter (`ua-`), and always ends with an alphanumeric (`-t`). Field values pass through `fit()`, which lowercases, replaces invalid characters with dashes, and truncates over-width values to `width - 5` characters plus a 4-character content hash. `<type>` is a 3-character role code:

| Code | Role |
|---|---|
| `pod` | DAG pod — persistent or ephemeral, including sweep entries |
| `svc` | Service |
| `crd` | Resource-step object (arbitrary Kubernetes resource) |
| `cln` | Per-ephemeral cleanup |
| `tdn` | Teardown |
| `ftd` | Finally-teardown |

For example, a persistent vLLM server for test `002` on node `wrk-4` has step name `002-guidellm-wrk-4-vllm-server` and resource name `ua-002-pod-vllm-server------wrk-4--------------------t`. Setup and finally support pods (builder, aggregator) keep fixed names from `config.yaml` and do not use this scheme.

### Failure Policies

Each test declares an `onFailure` policy in the test suite. The generator labels each step with its test's failure policy, and the runner applies it at execution time.

- **`continue`** — continue executing remaining steps within this test before proceeding to the next test. A failing step does not affect other steps or other sequences (nodes, sets).
- **`skipTest`** — skip remaining steps within this test in the failing sequence (tear down its resources), then proceed to the next test. Other sequences running the same test are unaffected.
- **`abort`** — all sequences complete the current test (pass or fail), then the runner skips every remaining test and runs teardown/cleanup. No further tests run.

### Output Structure

```
build/
├── manual/
│   ├── manifests/
│   │   ├── apply-configmap.yaml                         ← setup manifest
│   │   ├── create-builder.yaml                          ← setup manifest
│   │   ├── 001-component-wrk-4-test-runner.yaml         ← test manifest
│   │   ├── 001-component-wrk-6-test-runner.yaml
│   │   ├── 002-guidellm-wrk-4-vllm-server.yaml          ← persistent DAG manifest
│   │   ├── 002-guidellm-wrk-6-vllm-server.yaml
│   │   ├── 002-guidellm-wrk-4-pass-fail.yaml            ← sweep entry manifest
│   │   ├── 002-guidellm-wrk-6-pass-fail.yaml
│   │   ├── ...
│   │   └── create-aggregator.yaml                       ← teardown manifest
│   ├── 01-apply-configmap.sh                            ← apply script
│   ├── 02-create-builder.sh                             ← apply script
│   ├── 03-build.sh                                      ← exec script
│   ├── 04-001-component-wrk-4-test-runner.sh            ← apply script (parallel nodes share counter)
│   ├── 04-001-component-wrk-6-test-runner.sh
│   ├── ...
│   ├── 07-002-guidellm-wrk-4-vllm-server.sh             ← apply script
│   ├── 07-002-guidellm-wrk-6-vllm-server.sh
│   ├── 08-002-guidellm-wrk-4-pass-fail.sh               ← apply script
│   ├── 08-002-guidellm-wrk-6-pass-fail.sh
│   ├── ...
│   ├── NN-create-aggregator.sh                          ← apply script
│   ├── N-aggregate.sh                                   ← exec script
│   └── N-cleanup.sh                                     ← delete-all script
└── steps.json                                           ← serialized step list (+ metadata)
```

Manifests (`.yaml`) are written to `manual/manifests/` without a counter prefix — they are data files, not actions. Numbered shell scripts (`.sh`) are written to `manual/` and are what the operator runs in order: apply scripts reference the corresponding manifest (`oc apply -f manifests/<name>.yaml`), exec scripts run commands, and delete scripts clean up resources. Steps that run in parallel across nodes share the same counter. The counter is zero-padded to the width of the total step count so that shell glob ordering (`*.sh`) matches execution order. The numbered scripts are the single source of "what to do, in what order."

`<test_id>` is the zero-padded 3-digit position of the test in the test suite list (e.g. `001`, `002`). The same test can appear multiple times in the list (e.g. with different configs or failure policies), so `<test_id>` prevents collisions in resource names and results paths, while `<test_name>` provides readability. For node-scoped tests, `<node>` is added to prevent collisions across parallel nodes. For cluster-scoped multi-set tests, the zero-padded 4-digit `<set>` segment prevents collisions across sequential sets. Project-scoped and cluster-scoped single-set tests have no node or set segment. Services are named through the same `build_resource_name()` scheme using the `svc` type code, so their `metadata.name` is DNS-1035 compliant (starts with `ua-`, a letter). Service URL references in env vars and commands are automatically rewritten to match.

## Execution

### Execution Model

The manual writer emits the step list as numbered shell scripts under `build/manual/`, run in order. `scripts/manual_runner.py` drives them interactively and `scripts/auto_runner.py` drives them unattended; both read the same `steps.json` and `manual/*.sh` output. Steps that share a sequence number run in parallel (e.g. one per node for node-scoped tests). The runner applies each test's failure policy as it goes (see [Failure Policy Handling](#failure-policy-handling)).

#### Run Order

```
apply-configmap → create-builder → build → [test steps] → teardown: create-aggregator → aggregate → cleanup
```

Setup runs first, then each test's steps in test-suite list order, and finally the global teardown steps (create-aggregator, aggregate, cleanup), which always run regardless of earlier failures.

When a peer namespace is configured and any test uses `peer: true` DAG steps, setup also includes peer infrastructure — `apply-peer-configmap → create-peer-builder → peer-build` — and teardown includes a second aggregate-and-cleanup pass (create-peer-aggregator, peer-aggregate, peer-cleanup) targeting the peer namespace.

**1. Apply ConfigMap** — creates a ConfigMap containing all Go source, cluster config, test suite config, build script, and aggregator script.

**2. Create builder pod** — a long-lived Go toolchain pod with the PVC mounted at `/uat_workspace` and the ConfigMap mounted at `/src/`.

**3. Build binaries** — copies source from ConfigMap mounts into the PVC, generates a `go.mod` with the Ginkgo version pinned in `config.yaml`, and compiles one Ginkgo binary per unique test name at `/uat_workspace/<test>/test.bin`. If the same test name appears multiple times in the test suite (e.g. with different failure policies), all instances share the same binary.

**4. Tests** — each test contributes its steps to the run in list order. Scope determines the shape:

- **Node** tests produce one step sequence per target node, all running in parallel (sequences for the same test share sequence numbers). Within each sequence, steps run in order. The `finally-teardown` is the last step in each sequence and always runs regardless of earlier failures. Pods are pinned to the target node via `nodeSelector` in the pod manifests.

  ```
  wrk-6: A₆ → B₆ → C₆ → teardown₆ → finally-teardown₆   ┐
  wrk-4: A₄ → B₄ → C₄ → teardown₄ → finally-teardown₄   ┘ (parallel)
  → Next test
  ```
- **Cluster** tests orchestrate steps across nodes. Placement is controlled by the suite entry's `placement` section: `setType` (`permutation` or `combination`), `setSize` (how many distinct nodes per run), `setSelection` (which node sets to run), `setRequirements` (filters the node list by `componentValidation.sanity` fields), and `setCutoff` (limits the number of sets; ignored when `setSelection` is `random`). When `setSelection: all`, the test runs once per node set — each set is a self-contained DAG cycle (deploy, test, cleanup, teardown, finally-teardown) running sequentially. When `setSelection: random`, a single random set is chosen. For `setSize > 1`, DAG step *i* gets a `nodeSelector` pinning it to node *i* of the set. For `setSize == 1`, all DAG steps share the same node. Each set has its own `finally-teardown` — sets are treated as independent test runs. Multi-set runs include a zero-padded 4-digit `<set>` segment in step names to avoid collisions, and a `chain` label on all pods and services (e.g., `chain=0000`) so that cleanup selectors scope teardown to the current set — preventing leakage between sets if a teardown fails. Single-set runs omit both the set segment and the `chain` label. Sets run one after another, each fully completing before the next begins.

  ```
  setType: permutation, setSize: 2, setSelection: all, 3 nodes (6 sets, sequential):
    0000 (A→B): server₀ → client₀ → cleanup₀ → teardown₀ → finally-teardown₀
      → 0001 (A→C): server₁ → client₁ → cleanup₁ → teardown₁ → finally-teardown₁
      → ... → Next test

  setType: combination, setSize: 2, setSelection: all, 3 nodes (3 sets, sequential):
    0000 {A,B}: server₀ → client₀ → cleanup₀ → teardown₀ → finally-teardown₀
      → 0001 {A,C}: server₁ → client₁ → cleanup₁ → teardown₁ → finally-teardown₁
      → 0002 {B,C}: server₂ → client₂ → cleanup₂ → teardown₂ → finally-teardown₂
      → Next test

  setSelection: random, setSize: 2 (single set, no set prefix):
    server → client → cleanup → teardown → finally-teardown → Next test
  ```
- **Project** tests produce a single step sequence, without node affinity. Pods run without `nodeSelector`, validating project-wide concerns (quotas, RBAC, network policies). Step names follow the cluster/project convention: `<test_id>-<test>-<dag_step>`.

  ```
  004-quota-runner → 004-quota-cleanup-runner → 004-quota-finally-teardown → Next test
  ```

Every test, regardless of scope, ends with its `finally-teardown`. Tests run one after another in list order — the next test's steps begin only after the current test's teardown completes.

Because each test's steps go into the run in list order, scopes can be freely interleaved (e.g. node test → cluster test → node test) without any grouping constraints.

#### Failure Policy Handling

Each test declares an `onFailure` policy (`continue`, `skipTest`, `abort`). The runner reads each step's policy from `steps.json` and applies it as the test's steps execute. Lifecycle steps — per-ephemeral cleanup, teardown, and finally-teardown — always run regardless of policy, so resources are torn down even after a failure.

- **`continue`** — every step of the test runs regardless of failures. The runner records the failure and proceeds to the next test.

- **`skipTest`** — when a test step fails, the remaining test steps in that sequence are skipped. Lifecycle steps still run. Other sequences (other nodes/sets) are unaffected, and the runner proceeds to the next test.

- **`abort`** — behaves like `skipTest` within the failing test (remaining test steps skipped, lifecycle steps still run), then the runner skips straight to cleanup. No further tests run; aggregation and the global teardown still run.

Setup steps carry no policy and are treated as fatal: if a setup step fails, the run stops (there is nothing to test against).

```
continue policy (2 nodes):
  wrk-6: A₆ → B₆ → C₆ → teardown₆ → finally-teardown₆
  wrk-4: A₄ → B₄ → C₄ → teardown₄ → finally-teardown₄   → Next test
  (all steps run regardless of failures)

skipTest policy (2 nodes):
  wrk-6: A₆ → B₆ → C₆ → teardown₆ → finally-teardown₆
  wrk-4: A₄ → B₄ → C₄ → teardown₄ → finally-teardown₄   → Next test
  (remaining test steps skipped after a failure · teardown always runs)

abort policy (2 nodes):
  wrk-6: A₆ → B₆ → C₆ → teardown₆ → finally-teardown₆
  wrk-4: A₄ → B₄ → C₄ → teardown₄ → finally-teardown₄   → run stops
  (remaining test steps skipped after a failure · teardown always runs)
```

**5. Teardown** — after all tests complete (success or failure), the global teardown steps run per namespace, in order:

  1. **Aggregate results** — creates an aggregator pod, then execs into it to read individual JUnit/JSON reports and generate a consolidated report. Must complete before cleanup.
  2. **Cleanup** — deletes all pods and services matching the managed-by label, and the ConfigMap.

  When a peer namespace is in use, the same aggregate-then-cleanup sequence also runs in the peer namespace.

#### Test Sequences

A **sequence** is a linear run of steps that executes one complete DAG cycle: deploy resources, run tests, collect results, and clean up. Sequences are the fundamental unit of execution — every test produces one or more sequences, and every sequence is self-contained with its own resources, results, and cleanup.

**Sequence multiplicity and execution model:**

| Scope | Sequences per test | Execution | Node affinity |
|---|---|---|---|
| Node | One per target node | Parallel — all sequences run concurrently | Each sequence's pods pinned to one node via `nodeSelector` |
| Cluster | One per node set | Sequential — each set completes fully before the next begins | DAG step *i* pinned to node *i* of the set via `nodeSelector` |
| Project | One | Single sequence | No `nodeSelector` — pods run wherever the scheduler places them |

For node-scoped tests, the number of sequences equals the number of target nodes. For cluster-scoped tests, the number of sequences depends on placement config: `setSelection: all` produces one sequence per generated set (P(n,k) for permutations, C(n,k) for combinations, clamped by `setCutoff`); `setSelection: random` always produces one sequence. For project-scoped tests, there is always exactly one sequence.

**Sequence lifecycle:**

Every sequence follows the same lifecycle regardless of scope. DAG steps are processed in their definition order, and persistent, ephemeral, and resource steps can be interleaved freely. Phases 4–6 are **lifecycle steps** — they are generated automatically, carry `lifecycle: true` metadata, and always run regardless of failure policy:

1. **Resource deploy** — For each DAG step with a `resourceConfig`: generate a manifest for the specified Kubernetes resource (using `apiVersion`, `kind`, and `spec` from the config) and apply it. The resource is treated as persistent for teardown purposes — the resource type (e.g. `InferencePool`) is added to the teardown resource type list. Resource steps do not produce pods and have no cleanup phase.

2. **Persistent deploy** — For each pod DAG step with `persistsThroughSweep: true`: create the pod (and optional Service), wait for readiness. The resource stays up for all subsequent steps in this sequence to use. Multiple persistent steps are deployed in order as they appear in the DAG.

3. **Ephemeral run** — For each pod DAG step with `persistsThroughSweep: false` (the default): apply a test pod (and optional Service), wait for completion. If the step has a `parameterSweep`, one pod is created per sweep entry, run sequentially. Results write to the PVC. Ephemeral steps can reference persistent resources (e.g. a test client hitting a persistent server).

4. **Per-ephemeral cleanup** — Immediately after each ephemeral pod completes (success or failure), a cleanup step deletes that pod and its service by label. This releases resources like GPUs for subsequent steps without affecting persistent resources. Cleanup steps are paired 1:1 with ephemeral steps.

5. **Teardown** — After all DAG steps complete, a teardown step removes all persistent resources for this sequence. The default resource type list is `pods,services`; if any resource steps are present, each resource type (e.g. `InferencePool`) is appended to this list. All resources matching the sequence's labels are deleted.

6. **Finally-teardown** — The last step in the sequence. A safety net that catches anything teardown missed or anything left behind when earlier steps were skipped by failure policy. Uses the same resource type list as teardown. Deletes all resources — persistent, ephemeral, and resource-step-created — matching the sequence's labels. Runs unconditionally, even when preceding steps were skipped.

Lifecycle steps — per-ephemeral cleanup, teardown, and finally-teardown — run unconditionally regardless of failure policy. Only test steps (resource deploys, persistent deploys, and ephemeral runs) are skipped under `skipTest`/`abort`. Teardown is only added when the sequence has persistent or resource steps; by the time it runs, per-ephemeral cleanup has already handled ephemeral resources. Finally-teardown is always present — both use the same broad selector, but finally-teardown catches anything missed by earlier steps or left behind when steps were skipped by failure policy. For cluster-scoped tests, this guarantees a clean slate between sets.

When a sequence contains `peer: true` DAG steps, teardown and finally-teardown are generated separately for each namespace. The primary-namespace teardown only deletes primary resources; a peer teardown (suffixed `-peer`) deletes resources in the peer namespace. This prevents cleanup selectors from crossing namespace boundaries.

**Test sequencing:**

After all sequences for a test complete, the runner applies the test's failure policy and moves on to the next test (see [Failure Policy Handling](#failure-policy-handling)). The next test's first steps begin only after the current test's sequences have finished.

```
node-scoped (parallel sequences):
  seq(wrk-6): ... → finally-teardown₆
  seq(wrk-4): ... → finally-teardown₄   → next test

cluster-scoped (sequential sequences):
  set(0000): ... → finally-teardown₀
    → set(0001): ... → finally-teardown₁
    → ... → next test

project-scoped (single sequence):
  seq: ... → finally-teardown → next test
```

**Writer transparency:**

All scopes produce the same `Step` format. Placement is fully resolved during step computation — nodeSelectors, node labels, and set indices are baked into the rendered manifest content. The writer uses step metadata (scope, sequence keys) to determine execution ordering.

**Sequence structure examples:**

```
node-scoped sequence (one of N parallel sequences):
  002-guidellm-wrk-4-vllm-server                        [persistent deploy]
    → 002-guidellm-wrk-4-pass-fail                       [ephemeral run]
    → 002-guidellm-wrk-4-cleanup-pass-fail               [per-ephemeral cleanup]
    → 002-guidellm-wrk-4-sweep-short-burst               [ephemeral run (sweep entry)]
    → 002-guidellm-wrk-4-cleanup-sweep-short-burst       [per-ephemeral cleanup]
    → 002-guidellm-wrk-4-sweep-sustained-load            [ephemeral run (sweep entry)]
    → 002-guidellm-wrk-4-cleanup-sweep-sustained-load    [per-ephemeral cleanup]
    → 002-guidellm-wrk-4-teardown                        [teardown]
    → 002-guidellm-wrk-4-finally-teardown                [finally-teardown, always runs]

cluster-scoped sequences (setSize: 2, setSelection: all — sequential):
  0000: 003-network-0000-iperf-server                     [persistent deploy]
    → 003-network-0000-iperf-client                       [ephemeral run]
    → 003-network-0000-cleanup-iperf-client               [per-ephemeral cleanup]
    → 003-network-0000-teardown                           [teardown]
    → 003-network-0000-finally-teardown                   [finally-teardown]
  → 0001: 003-network-0001-iperf-server
    → 003-network-0001-iperf-client
    → 003-network-0001-cleanup-iperf-client
    → 003-network-0001-teardown
    → 003-network-0001-finally-teardown
  → ...

project-scoped sequence (single, no nodeSelector):
  004-quota-check-runner                                  [ephemeral run]
    → 004-quota-cleanup-check-runner                      [per-ephemeral cleanup]
    → 004-quota-finally-teardown                          [finally-teardown]
```

## Results

Each test run writes JUnit XML and benchmark output to the PVC in a flat directory structure. Each step's workspace directory is named after its step name — the same name used for pod names and filenames:

```
<base-path>/<run-id>/
├── binaries/
│   ├── component/test.bin
│   └── guidellm/test.bin
├── 001-component-wrk-4-test-runner/
│   └── junit.xml
├── 001-component-wrk-6-test-runner/
│   └── junit.xml
├── 002-guidellm-wrk-4-vllm-server/           # persistent DAG pod workspace
├── 002-guidellm-wrk-4-pass-fail/
│   └── junit.xml
├── 002-guidellm-wrk-4-sweep-short-burst/
│   ├── junit.xml
│   └── results.json
├── 002-guidellm-wrk-4-sweep-sustained-load/
│   └── junit.xml
├── 002-guidellm-wrk-6-vllm-server/
├── 002-guidellm-wrk-6-pass-fail/
│   └── junit.xml
├── ...
├── 003-network-0000-iperf-server/              # cluster-scoped, set 0
├── 003-network-0000-iperf-client/
│   └── junit.xml
├── 003-network-0001-iperf-server/              # cluster-scoped, set 1
├── 003-network-0001-iperf-client/
│   └── junit.xml
├── ...
├── 004-quota-check-runner/                     # project-scoped (no node segment)
│   └── junit.xml
└── report/
    └── summary.json
```

The base path is a cluster-level setting that scopes results to a particular test suite or environment (e.g. `uat/results`). The run id provides timestamp-based isolation between runs. Each step gets a flat directory named after its step name, which encodes the test index, test name, node (for node-scoped tests) or set index (for cluster-scoped multi-set tests), and DAG step for uniqueness and readability. Test pods write to `/uat_workspace` and files land in the right place via Kubernetes `subPath` mounting.

### Aggregation

The aggregator pod runs `scripts/aggregate.py` (deployed via the ConfigMap) against the results directory. It walks all step directories, skipping `binaries/` and `report/`, and parses each `junit.xml` file to extract `tests`, `failures`, `errors`, and `skipped` counts. Each directory produces a per-entry result with a `passed`/`failed` status (failed if any failures or errors). The aggregator writes `report/summary.json` with this structure:

```json
{
  "status": "passed",
  "totals": {"tests": 42, "failures": 0, "errors": 0, "skipped": 2},
  "entries": [
    {"name": "001-component-wrk-4-test-runner", "tests": 12, "failures": 0, "errors": 0, "skipped": 0, "status": "passed"},
    ...
  ]
}
```

The top-level `status` is `"failed"` if any entry has failures or errors, `"passed"` otherwise. The summary is also printed to stdout for operator visibility.

## Peer Namespace

Some tests validate cross-namespace behavior (e.g. DNS isolation, network policy enforcement). To support this, the cluster config can declare a `peerNamespace` and optional `peerStorage`. Any DAG step with `peer: true` deploys to the peer namespace instead of the primary namespace, using the peer storage config for its PVC, base path, and models volume.

The peer namespace gets its own independent infrastructure: a separate ConfigMap, builder pod, and aggregator pod, each targeting the peer namespace's PVC. This keeps the two namespaces fully isolated at the Kubernetes level while the test DAG orchestrates across both. Teardown is also split: each namespace gets its own teardown and finally-teardown steps so that cleanup selectors do not cross namespace boundaries.

Pod name uniqueness is scoped per namespace: two pods with the same name in different namespaces do not collide. Each step carries a target namespace; the writer uses it to render namespace-specific commands.

## Design Decisions

| Decision | Rationale |
|---|---|
| Steps-first generation | The generator computes a flat, ordered step list from test definitions, then the manual writer derives its output from that list. This keeps step computation independent of output format, and makes it straightforward to add writers for other orchestration harnesses without changing step computation. |
| Three test scopes, one list | **Node** tests validate per-node hardware (GPUs, drivers). **Cluster** tests validate multi-node coordination (RDMA, interconnect) with configurable placement at the suite level. **Project** tests validate namespace-level concerns (quotas, RBAC) without node affinity. Each test definition declares which scopes it supports; the same test can appear with different (supported) scopes across suites. All three scopes are declared in a single ordered list in the test suite, allowing interleaved execution: each test is its own entry in the run, so scopes can alternate freely. |
| Two-name scheme: readable step names, fixed-width resource names | Each step carries a human-readable **step name** (used for filenames and PVC paths) and a **resource name** (used for Kubernetes `metadata.name`). Step names follow a single convention: `<test_id>-<test>-<node>-<dag_step>` (node-scoped), `<test_id>-<test>-<set>-<dag_step>` (cluster-scoped, multiple sets), or `<test_id>-<test>-<dag_step>` (cluster single set, or project-scoped), with `-<id>` appended for sweep entries. Lifecycle steps extend the convention with a fixed suffix: `<prefix>-cleanup-<dag_step>[-<id>]` (per-ephemeral cleanup), `<prefix>-teardown`, and `<prefix>-finally-teardown`. Resource names are generated separately by `build_resource_name()` as a fixed-width positional string (`ua-<test_id>-<type>-<step>-<node>-<set>-<sweep>-t`), each field padded with `-` and passed through `fit()` (sanitize + hash-truncate). This guarantees every resource name is RFC 1123 / DNS-1035 valid and bounded at 54 characters (34 for CRDs), regardless of how long the test or DAG-step names are. `<test_id>` (zero-padded, 3-digit) prevents collisions when the same test appears multiple times in the suite; `<node>` prevents collisions across parallel nodes; `<set>` (zero-padded, 4-digit) prevents collisions across node sets. Services use the `svc` type code in the resource-name scheme; service URL references in env vars and commands are rewritten automatically to match. |
| Placement is step computation, not writer logic | All scopes resolve placement during step computation — nodeSelectors and labels are baked into the rendered manifest content. The resulting step list uses the same `Step` format across all scopes. The writer uses step metadata (scope, sequence keys) to determine execution ordering. |
| One binary per test, not per parameter | Same test logic, different runtime config. Avoids redundant compilation. |
| ConfigMap → Builder Pod → PVC | A single ConfigMap delivers all Go source to the builder pod. Builder pod provides a persistent compilation environment. PVC makes binaries accessible to any test container. Delivery mechanism is swappable (GitHub pull, custom image) without changing the rest of the run. |
| Peer namespace with independent infrastructure | Cross-namespace tests need resources in two namespaces, but Kubernetes RBAC, ConfigMaps, and PVCs are namespace-scoped. Rather than granting cross-namespace access, the peer namespace gets its own ConfigMap, builder pod, and aggregator pod. This keeps the two namespaces fully isolated at the Kubernetes level. The test DAG orchestrates across both by tagging individual DAG steps with `peer: true`. Pod name uniqueness is scoped per namespace so that mirrored step names (e.g. a server in each namespace) do not collide. |
| Separate models storage | Model weights live on a dedicated volume (`storage.models`) rather than the results PVC. This keeps large model files (tens of GB) out of the per-run results directory, allows a single pre-populated cache to be shared across runs, and lets inference servers load weights from `/models` without downloading at runtime. The models volume is mounted read-only on all DAG pods when configured. The `ModelsStorageConfig` is its own model so the backing store can be extended beyond PVC (e.g. object storage) without changing `StorageConfig`. |
| DAG resources persist through sweep | Expensive resources (GPU-backed servers) deploy once; the parameter sweep reuses them. |
| Resource steps for non-pod K8s objects | DAG steps with `resourceConfig` deploy arbitrary Kubernetes resources (InferencePools, ConfigMaps, etc.) as part of the test DAG. They are treated as persistent for teardown purposes — each resource type (e.g. `InferencePool`) is added to the teardown resource type list so cleanup catches them. This avoids coupling the harness to a fixed set of Kubernetes resource types. |
| One step per DAG step | Each non-persistent DAG step gets its own generated step (not one per test). Sweep iterations each get a separate test pod and step, keeping the execution order explicit. |
| Resource validation at generation time | Before step computation, the generator validates that each target node has sufficient resources for the test's peak concurrent demand (sum of persistent + max ephemeral). Uses `componentValidation.sanity` fields as the capacity source — any field whose name matches a Kubernetes resource type (e.g., `nvidia.com/gpu`) is compared against the rendered DAG step resource requests. Resource steps are excluded from this check (they don't have resource requests). Catches over-subscription at generation time rather than producing manifests that fail to schedule. |
| Ephemeral pod cleanup after each step | Non-persistent pods are deleted immediately after completion to release resources (e.g. GPUs) for subsequent steps. Each ephemeral step's pod and service carry a `sweep` label for targeted deletion without affecting persistent resources. |
| One sequence per unit of work | Each test produces one or more step sequences: one per node (node-scoped), one per set (cluster-scoped), or one total (project-scoped). Node-scoped sequences are independent (their steps can run in parallel); cluster-scoped sequences run sequentially (each set completes before the next begins). Different tests always run in sequence. |
| Per-test failure policy | Each test declares its own `onFailure` (`continue`, `skipTest`, `abort`) instead of a single global flag. The runner enforces the policy after each test: `continue` runs every step regardless of failures; `skipTest` skips the remaining steps in a failing sequence (tearing down its resources) but leaves other sequences and subsequent tests unaffected; `abort` lets the current test finish, then skips every remaining test and runs teardown/cleanup. Each sequence's `finally-teardown` step always runs, cleaning up resources even when earlier steps were skipped. |

## Constraints

- **ConfigMap 1MB limit**: all Go source, cluster config, test suite config, build script, and aggregator script are packed into a single ConfigMap. A project with many tests may exceed Kubernetes' 1MB ConfigMap limit.
- **Resource name width**: resource names use a fixed-width positional scheme (`build_resource_name()`), so they are always ≤54 characters (34 for CRDs) and DNS-1035 valid — the 63-character Kubernetes name limit can no longer be exceeded. Over-width field values (long test, node, or DAG-step names) are hash-truncated by `fit()` (`width - 5` chars + a 4-character content hash) rather than overflowing.
- **Suite and set caps**: a suite may contain at most 999 tests (`test_id` is 3 digits), and a single cluster-scoped test may generate at most 9999 sets (`set` is 4 digits). The generator aborts if either limit is exceeded.
- **One run per namespace**: the builder pod has a fixed name, so only one run can execute at a time in a given namespace. This is typically sufficient — the step sequences are the element that scales with cluster size, and a single run fans out to all target nodes.
- **Sequential sweeps**: parameter sweep entries within a test run as separate pods in sequence. Failure behavior is controlled per-test via the `onFailure` field in the test suite (`continue`, `skipTest`, or `abort`). The runner applies the policy after each test: `continue` runs every step through failures, `skipTest` skips the remaining steps in a failing sequence, and `abort` finishes the current test then skips the remaining tests, still running teardown/cleanup. When running the generated scripts manually, they are independent and the operator controls whether to proceed.
- **Combinatorial growth for cluster tests**: `setSelection: all` generates P(n, k) sets for permutations or C(n, k) for combinations, where n is the number of cluster nodes and k is `setSize`. Each set runs as a complete DAG cycle. For large clusters with `setType: permutation` and high `setSize`, the number of sets grows factorially — e.g. 10 nodes with `setSize: 3` produces 720 permutations. Use `setSelection: random` or `setType: combination` (which produces 120 for the same parameters) to bound the run count. As a hard stop, the generator aborts if a test produces more than 9999 sets.
