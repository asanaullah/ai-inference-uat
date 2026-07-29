<!-- Assisted by Claude Opus 4.6 -->
# AI Inference UAT Harness
A declarative test harness that generates Kubernetes manifests from test definitions. Given a cluster configuration (target nodes, namespace, and storage) and a test suite, the generator produces both manually-executable manifests, as well as Tekton pipeline manifests for automated execution on OpenShift.

## Table of Contents

- [How It Works](#how-it-works)
  - [Generation](#generation)
  - [Intermediate DAG (steps.json)](#intermediate-dag-stepsjson)
  - [Execution Flow](#execution-flow)
  - [Test Scopes](#test-scopes)
    - [Placement (Cluster Scope)](#placement-cluster-scope)
  - [PVC Directory Hierarchy](#pvc-directory-hierarchy)
- [Quickstart](#quickstart)
  - [Prerequisites](#prerequisites)
  - [Install](#install)
  - [Generate Manifests](#generate-manifests)
  - [CLI Options](#cli-options)
  - [Run Manually](#run-manually)
  - [Run with Tekton (untested)](#run-with-tekton-untested)
- [Adding a Custom Test](#adding-a-custom-test)
- [Test Definition Reference](#test-definition-reference)
  - [DAG Steps](#dag-steps)
  - [Template Variables](#template-variables)
  - [Parameter Sweeps](#parameter-sweeps)
  - [DAG Pods with Services](#dag-pods-with-services)
  - [Spec Override](#spec-override)
- [Configuration](#configuration)
  - [Cluster Config](#cluster-config-clusternameyaml)
  - [Tool Config](#tool-config-configyaml)
- [Extensibility](#extensibility)
- [Admin Usage](#admin-usage)
- [Project Structure](#project-structure)




## How It Works

The generator reads test definitions (YAML + Go source), a cluster config describing target nodes, and a tool config. It produces two equivalent output formats from the same internal representation:

- **Manual output** (`build/manual/`) — numbered shell scripts in execution order, plus a `manifests/` subdirectory with the YAML manifests they reference. Run the scripts in order with `bash`.

- **Tekton output** (`build/tekton/`) — a single flat Tekton Pipeline with all tasks as direct entries, plus a PipelineRun. Pod manifests are embedded directly in task scripts. Apply the entire directory to run the full suite automatically.

Both outputs are derived from the same ordered step list, ensuring the underlying Kubernetes workloads (pods, services, configmaps) are equivalent regardless of execution method.


```
                                                                    ┌→ Manual Manifests
Test Definitions (YAML + Go) + Node List → python -m src → Steps ──┤                     → OpenShift Execution → Results on PVC
                                              ↑                     └→ Tekton Manifests
                                              │
                                       steps.json (optional re-entry point)
```

### Generation

The generator separates step computation (what to run) from writers (how to run it). Writers are independent consumers of the same step list, so adding a new execution backend means writing a new writer — step computation doesn't change.

1. **Step computation** — converts test definitions into a flat, ordered list of steps. Each step is either a resource to create (pod manifest, optionally bundled with a service) or an action to execute (apply a manifest, exec into a pod, delete resources). Ordering is implicit in list position. Both output layers consume the same step list.

2. **Manual writer** — writes the steps as standalone files to `build/manual/`, organized by phase (setup, test, teardown). Numbered `.sh` scripts in `manual/` are what the operator runs in order. Manifests are written to `manual/manifests/` as data files — each apply script references its manifest via `oc apply -f manifests/<name>.yaml`.

3. **Tekton writer** — derives Tekton Tasks and a single flat Pipeline from the same steps. Pod manifests are embedded directly in Tekton Task scripts, so `build/tekton/` is self-contained. The writer assigns `onError` values, `when` guards, and guard tasks based on each test's failure policy.

### Intermediate DAG (steps.json)

After step computation and before writing output, the generator serializes the full step list to `build/steps.json`. This file captures the complete DAG — setup, test, and teardown steps — along with the tool and cluster config used to produce them.

The generator can also consume `steps.json` as input via `--steps`, skipping config loading and step computation entirely:

```bash
# Normal: compute steps from config, write steps.json + manual + tekton
python -m src --test-suite examples/minimal/test_suite.yaml --test-lib examples/minimal --cluster cluster/ocp-test.yaml

# From steps: load steps.json, write manual + tekton
python -m src --steps build/steps.json
```

This enables a two-phase workflow for custom step injection:

1. Generate `steps.json` from config
2. Edit the file — add, remove, or reorder steps
3. Re-run with `--steps` to produce output from the modified DAG

The file is validated on load with Pydantic (field types, metadata structure) and structural checks (unique pod names, source references point to existing generate steps, valid command and probe values, service name DNS-1035 compliance, and failure policy labels).

### Execution Flow

```
Cluster Pipeline (single flat pipeline):
  Setup:    apply-configmap → create-builder → build
  Tests:    [tests in test_suite.yaml list order]
              node-scoped: parallel task chains (one per node), chained via runAfter
              cluster-scoped: one task chain per node set (sequential)
              project-scoped: single task chain (no node affinity)
              each test ends with a guard task (fan-in sync point)
  Finally:  create-aggregator → aggregate → cleanup
```

Each test produces one or more task chains depending on scope: one per node (node-scoped, parallel, pinned via `nodeSelector`), one per node set (cluster-scoped, sequential, pinned via `nodeSelector`), or a single chain (project-scoped, no `nodeSelector`). All chains follow the same lifecycle:

```
Task chain per node:
  persistent: deploy pod, wait for readiness (stays up)
  ephemeral:  run test pod → cleanup (per sweep entry, releases resources)
  after all DAG steps: teardown persistent pods
  finally-teardown: safety-net (always runs, no when guard)
Guard task: fans in after all nodes' chains, checks for failures
```

### Test Scopes

Tests are organized into three scopes based on where and how they run:

- **Node** — validates individual nodes in isolation. Each node-scoped test runs independently on every target node listed in the cluster config, pinned via `nodeSelector`. All node task chains execute in parallel. Use for hardware validation, GPU diagnostics, driver checks, and single-node inference benchmarks.

- **Cluster** — validates behavior that spans multiple nodes but still requires node pinning. Cluster-scoped tests use a `placement` config to filter eligible nodes, generate node sets (combinations or permutations), and pin each DAG step to a specific node within the set. Sets run sequentially — each completes fully before the next begins. Use for multi-node coordination tests like distributed training, inter-node networking, or GPU-to-GPU communication across nodes.

- **Project** — validates namespace-level resources with no node affinity. Project-scoped tests run as a single chain without `nodeSelector`, letting the scheduler place pods freely. Use for namespace quota checks, RBAC validation, service mesh configuration, or any test that operates at the project level rather than targeting specific hardware.

Tests are registered in `test_suite.yaml` as an ordered list, each with a scope and failure policy:

```yaml
spec:
  tests:
    - name: platform-check
      scope: project
      onFailure: continue

    - name: component
      scope: node
      onFailure: continue

    - name: inference
      scope: node
      onFailure: abort
      timeout: 1200

    - name: iperf3
      scope: cluster
      onFailure: continue
```

The `onFailure` field controls what happens when a step within the test fails (default: `continue`):
- `continue` — all steps run regardless of failures. Guard task proceeds to the next test.
- `skipTest` — remaining steps in the failing test's chain are skipped (teardown still runs). Guard task proceeds to the next test. For node-scoped tests, only the failing node's chain is skipped; other nodes complete normally.
- `abort` — remaining steps in the failing test's chain are skipped. Guard task halts the pipeline. For node-scoped tests, only the failing node's chain is skipped; other nodes complete normally before the pipeline stops.

The optional `timeout` field (integer, seconds) overrides the `defaultTestTimeout` from `config.yaml` for this test's ephemeral pods.

#### Placement (Cluster Scope)

Cluster-scoped tests accept a `placement` config that controls how pods are distributed across nodes:

```yaml
spec:
  tests:
    - name: network
      scope: cluster
      onFailure: continue
      placement:
        setSize: 2
        setType: combination
        setSelection: all
        setCutoff: 3
        setRequirements:
          gpuCount: 4
```

| Field | Default | Description |
|---|---|---|
| `setSize` | `1` | Number of distinct nodes per set. When `> 1`, DAG step *i* is pinned to node *i* of the set |
| `setType` | `combination` | `combination` (unordered, {A,B} = {B,A}) or `permutation` (ordered, (A,B) ≠ (B,A)) |
| `setSelection` | `random` | `random` picks a single random set; `all` uses every generated set |
| `setCutoff` | `1` | Limits the number of sets when `setSelection: all`. `0` means no limit |
| `setRequirements` | `{}` | Filters eligible nodes by `componentValidation.sanity` fields. Numeric fields are minimums, string fields are exact matches |

### PVC Directory Hierarchy

Every DAG step gets a unique directory on the PVC, computed transparently by the generator. Step names encode all hierarchy information (test_id, test name, node, DAG step), so directories are flat under the timestamp. Test pods write to `/workspace` and files land in the right place via `subPath` mounting.

```
<basePath>/<timestamp>/
  binaries/
    <test_name>/test.bin
  <test_id>-<test>-<node>-<dag_step>/        (node-scoped)
    junit.xml
    ...
  <test_id>-<test>-set<i>-<dag_step>/         (cluster-scoped, multiple sets)
    junit.xml
  <test_id>-<test>-<dag_step>/               (cluster single set / project-scoped)
    junit.xml
  report/
    summary.json
```

DAG pods also get a second mount at `/binaries` for access to compiled test binaries.

## Quickstart

### Prerequisites

- Python 3.10+
- An OpenShift cluster with `oc` configured
- A PVC with **ReadWriteMany (RWX)** access mode (e.g. CephFS, NFS). Multi-node runs pin pods to different nodes that share one PVC — RWO block storage will fail at scheduling.
- Nodes labeled with `kubernetes.io/hostname`
- For Tekton: a service account with permissions to create/delete pods, services, deployments, configmaps, and exec into pods in the target namespace

### Install

```bash
pip install -r requirements.txt
```

### Generate Manifests

```bash
python -m src \
  --test-suite examples/minimal/test_suite.yaml \
  --test-lib examples/minimal \
  --cluster cluster/ocp-test.yaml \
  --config config.yaml \
  --templates-dir templates \
  --scripts-dir scripts
```

Output is written to `build/manual/` and `build/tekton/`.

### CLI Options

| Flag | Default | Description |
|---|---|---|
| `--test-suite` | (required\*) | Path to `test_suite.yaml` |
| `--test-lib` | (required\*) | Directory containing test definition YAMLs and Go source files |
| `--cluster` | (required\*) | Path to the cluster config YAML |
| `--config` | `config.yaml` | Path to the tool config |
| `--run-id` | `manual-run` | Timestamp substitute for manual output |
| `--output` | `build` | Output directory |
| `--templates-dir` | `templates` | Path to Jinja2 templates |
| `--scripts-dir` | `scripts` | Path to support scripts (e.g. `aggregate.py`) |
| `--steps` | | Path to a `steps.json` file; skips config loading and step computation |

\* Not required when `--steps` is provided.

### Run Manually

Scripts are numbered in execution order. Run them sequentially:

```bash
# Run all scripts in order
for f in build/manual/*.sh; do
  echo "Running $f"
  bash "$f"
done
```

Or run selectively — setup scripts come first, then test scripts (steps that run in parallel across nodes share the same counter), then teardown scripts at the end:

```bash
# Setup (first few scripts)
bash build/manual/01-apply-configmap.sh
bash build/manual/02-create-builder.sh
bash build/manual/03-build.sh

# Test scripts follow (numbered 04+)
# ...

# Teardown (final scripts)
bash build/manual/N-create-aggregator.sh
bash build/manual/N-aggregate.sh
bash build/manual/N-cleanup.sh
```

### Run with Tekton (untested)

```bash
oc apply -f build/tekton/
```

This creates all Tasks, the cluster Pipeline, and triggers a PipelineRun. Monitor with:

```bash
oc get pipelineruns -w
```

## Adding a Custom Test

Adding a test requires three things: an entry in `test_suite.yaml`, a test definition YAML, and a Ginkgo test file.

### 1. Register the Test

Add the test to `test_suite.yaml`:

```yaml
spec:
  tests:
    - name: component
      scope: node
      onFailure: continue
    - name: inference
      scope: node
      onFailure: abort
      timeout: 1200
    - name: my-test           # add here
      scope: node
      onFailure: continue
```

### 2. Create the Test Definition

Create `examples/minimal/my-test.yaml`:

```yaml
apiVersion: uat.openshift.io/v1
kind: Test
metadata:
  name: my-test
  version: v0.0.1
  description: Short description of what this test validates
spec:
  requirements:
    gpu: true           # set to false if no GPU needed

  source:
    ginkgo: my-test.go

  dag:
    - name: test-runner
      image: registry.redhat.io/ubi9/ubi:latest
      labelFilter: pass-fail
      env:
        - name: NODE_NAME
          value: '{{ nodeSpec.name }}'
      persistsThroughSweep: false
      parameterSweep: null
```

### 3. Write the Ginkgo Test

Create `examples/minimal/my-test.go`:

```go
package test

import (
    "testing"

    . "github.com/onsi/ginkgo/v2"
    . "github.com/onsi/gomega"
)

func TestMyTest(t *testing.T) {
    RegisterFailHandler(Fail)
    RunSpecs(t, "My Test Suite")
}

var _ = Describe("My Test", Label("pass-fail"), func() {
    It("should validate something", func() {
        Expect(true).To(BeTrue())
    })
})
```

The `Label("pass-fail")` must match the `labelFilter` in the test YAML. The compiled binary runs with `--ginkgo.label-filter=pass-fail` to select which specs execute.

### 4. Generate and Run

```bash
python -m src --test-suite examples/minimal/test_suite.yaml --test-lib examples/minimal --cluster cluster/ocp-test.yaml
```

The generated output compiles `my-test.go` into a binary on the builder pod and runs it on each target node.

## Test Definition Reference

### DAG Steps

Each test defines an ordered DAG of resources to deploy and run. Steps are either **persistent** (stay up for all sweep entries) or **ephemeral** (run once per sweep entry and exit). Ephemeral pods are cleaned up immediately after completion to release resources (e.g. GPUs) for subsequent steps. Each ephemeral pod carries a `sweep` label matching its sweep entry ID, enabling targeted deletion without affecting persistent pods. Persistent pods are torn down after all DAG steps complete.

| Field | Description |
|---|---|
| `name` | Step name, used in pod naming and directory hierarchy |
| `image` | Container image |
| `persistsThroughSweep` | `true`: pod stays up (e.g. inference server). `false`: pod runs to completion |
| `labelFilter` | Ginkgo label filter for the compiled binary |
| `command` | Structured command with `args` and `flags` |
| `parameterSweep` | If set, one pod per entry with merged flags |
| `service` | Service configuration block (see [DAG Pods with Services](#dag-pods-with-services)). Fields: `enabled` (bool, default `false`), `name` (string, used in `services["name"]` template lookups), `port` (int, default `8000`), `headless` (bool, default `true` — sets `clusterIP: None`) |
| `env` | Environment variables (values are Jinja2 templates) |
| `resources` | CPU/GPU/memory requests and limits |
| `readinessProbe` | Readiness probe for persistent pods |
| `ports` | Container ports |
| `privileged` | If `true`, runs with `securityContext.privileged` and `hostPID` |
| `volumeMounts` | Additional volume mounts (beyond the PVC) |
| `volumes` | Additional volume definitions |

### Template Variables

Available in `command`, `env`, and `resources` values via Jinja2:

| Variable | Description |
|---|---|
| `nodeSpec.*` | Full node spec from cluster config (e.g. `{{ nodeSpec.componentValidation.sanity.gpuCount }}`) |
| `serverConfig.*` | Test-level config dict (e.g. `{{ serverConfig.model }}`) |
| `services["name"].url` | URL of a DAG step's service |
| `paramSweep.id` | Current sweep entry ID |
| `paramSweep.command` | Resolved command list for the current sweep entry |
| `timestamp` | Run identifier (`__TIMESTAMP__` placeholder) |
| `node` | Target node name |

### Parameter Sweeps

A sweep runs the same test pod multiple times with different flags. Define a `baseCommand` and a list of `entries`:

```yaml
parameterSweep:
  baseCommand:
    args: [guidellm, benchmark, run]
    flags:
      target: '{{ services["vllm-server"].url }}'
      output-dir: /workspace
      max-seconds: 120
  entries:
    - id: short-burst
      description: Short high-rate burst
      flags:
        max-seconds: 30
        rate: 4
    - id: sustained-load
      description: Sustained moderate-rate
```

Each entry's `flags` are merged over `baseCommand.flags`. One pod is created per entry.

### DAG Pods with Services

Any DAG step — persistent or ephemeral — can have a Service. The most common use is a persistent pod (e.g. an inference server) that test pods connect to:

```yaml
dag:
  - name: vllm-server
    image: nvcr.io/nvidia/vllm:26.03-py3
    persistsThroughSweep: true
    service:
      enabled: true
      port: 8000
      name: vllm-server
      headless: true
    command:
      args: [python, -m, vllm.entrypoints.openai.api_server]
      flags:
        model: ibm-granite/granite-3.3-8b-instruct
        port: 8000
    readinessProbe:
      httpGet:
        path: /health
        port: 8000
      initialDelaySeconds: 30
      periodSeconds: 10

  - name: my-test
    image: my-test-image:latest
    persistsThroughSweep: false
    env:
      - name: SERVER_URL
        value: '{{ services["vllm-server"].url }}'
```

The generator creates a Kubernetes Service alongside the pod. Downstream steps reference it via `{{ services["vllm-server"].url }}`, which resolves to `http://svc-<test_id>-<test>-<node>-vllm-server:8000`. Service names are prefixed with `svc-` for DNS-1035 compliance. By default, services are headless (`clusterIP: None`) — traffic routes directly to the pod IP without kube-proxy load balancing. Set `headless: false` to create a standard ClusterIP service instead.

### Spec Override

Each suite entry can include a `spec` section that is deep-merged over the test definition's `spec` from `<test>.yaml`. This lets the same test definition produce different runtime configurations across suite entries without duplicating the test file:

```yaml
spec:
  tests:
    - name: inference
      scope: node
      onFailure: abort
      spec:
        serverConfig:
          model: ibm-granite/granite-3.3-8b-instruct
        dag:
          vllm-server:
            resources:
              limits:
                nvidia.com/gpu: 2
```

The merge is recursive for dict fields (`serverConfig`, `requirements`) — nested keys are merged, not replaced. For `dag` overrides, steps are referenced by name as dict keys (not a list): each key is matched to a DAG step by `name`, and only the specified fields within that step are overridden. Unmentioned fields and unmentioned DAG steps retain their `<test>.yaml` defaults. After merging, the result is re-validated as a `TestSpec`.

## Configuration

### Cluster Config (`cluster/<name>.yaml`)

Defines target nodes, namespace, and storage:

```yaml
spec:
  compliance:
    fipsEnabled: true

  nodes:
    - name: wrk-4
      componentValidation:
        sanity:
          gpuCount: 4
          gpuModel: NVIDIA-A100-SXM4-40GB
          # ... additional fields available as {{ nodeSpec.componentValidation.* }}
  namespace: my-namespace
  storage:
    pvc: my-pvc
    basePath: uat/results
```

The `compliance` section holds cluster-wide settings consumed by Go test binaries (e.g., the FIPS mode check in the component test). The harness passes these through via the embedded `cluster.yaml` — they are not used by the manifest generator.

The `name` field is the value matched against the `nodeSelectorKey` label (default: `kubernetes.io/hostname`). It is also used in step names for human readability. For Kubernetes resource names (pods, services, Tekton tasks), the generator sanitizes the node name: invalid characters are replaced with dashes, uppercase is lowercased, and names longer than 16 characters are truncated to 12 characters with a 4-character hash suffix. Short, simple names like `wrk-4` are used as-is; FQDN hostnames like `ip-10-0-1-42.ec2.internal` are automatically shortened.

All fields under `componentValidation` are available in Jinja2 templates. The `sanity.gpuCount` field is used for GPU requirement checks — tests with `requirements.gpu: true` are skipped on nodes with `gpuCount <= 0`.

### Tool Config (`config.yaml`)

Controls images, pod names, labels, and timeouts:

```yaml
oseCLIImage: registry.redhat.io/openshift4/ose-cli:latest
builderImage: golang:1.26
ginkgoVersion: v2.32.0
aggregatorImage: python:3-slim
configmapName: uat-test-source
builderPodName: ginkgo-builder
aggregatorPodName: uat-aggregator
nodeSelectorKey: kubernetes.io/hostname
managedByLabel: uat-generator
builderTimeout: 300
aggregatorTimeout: 120
deployTimeout: 600
defaultTestTimeout: 600
pipelineTimeout: 7200
finallyTimeout: 900
```

All timeout values are integers in seconds.

## Extensibility

### Per-Test Timeout and Failure Policy

Each test in `test_suite.yaml` can override the default timeout and declare its own failure policy. Tests can be listed in any order and the same test can appear multiple times with different settings:

```yaml
spec:
  tests:
    - name: inference
      scope: node
      onFailure: abort
      timeout: 1200        # override defaultTestTimeout from config.yaml

    - name: inference        # same test, different policy
      scope: node
      onFailure: continue
      timeout: 3600
```

### Custom Step Injection (steps.json)

The generator writes an intermediate `steps.json` after step computation. You can edit this file — add, remove, or reorder steps — then re-run with `--steps` to produce output from the modified step list. See [Intermediate DAG](#intermediate-dag-stepsjson) for details.

### Custom Node Attributes

The `componentValidation` section in cluster configs accepts arbitrary fields via Pydantic's `extra="allow"`. Any field you add is available in Jinja2 templates as `{{ nodeSpec.componentValidation.* }}` — no code changes needed.

```yaml
# cluster/my-cluster.yaml
spec:
  nodes:
    - name: wrk-4
      componentValidation:
        sanity:
          gpuCount: 4
          gpuModel: NVIDIA-A100-SXM4-40GB    # custom field
          nvlink: NV4                          # custom field
          cpuCount: 128                        # custom field
```

Use these in test definitions for resource requests, environment variables, or command flags:

```yaml
resources:
  limits:
    nvidia.com/gpu: '{{ nodeSpec.componentValidation.sanity.gpuCount }}'
env:
  - name: GPU_MODEL
    value: '{{ nodeSpec.componentValidation.sanity.gpuModel }}'
```

### Server Config

The `serverConfig` dict in test definitions passes arbitrary key-value pairs into the template context. Use it for test-specific configuration that varies between deployments:

```yaml
# test definition
spec:
  serverConfig:
    model: ibm-granite/granite-3.3-8b-instruct
    maxTokens: 4096
  dag:
    - name: server
      command:
        flags:
          model: '{{ serverConfig.model }}'
```

### Arbitrary Kubernetes Configuration in DAG Steps

DAG steps accept arbitrary dicts for `volumeMounts`, `volumes`, `env`, `ports`, `resources`, and `readinessProbe`. These map directly to Kubernetes pod spec fields, so you can mount secrets, define custom probes, or request specialized hardware without modifying the generator:

```yaml
dag:
  - name: my-step
    volumeMounts:
      - name: model-cache
        mountPath: /models
    volumes:
      - name: model-cache
        hostPath:
          path: /mnt/models
    readinessProbe:
      exec:
        command: [cat, /tmp/ready]
```

### Custom Templates

The `--templates-dir` CLI arg loads all Jinja2 templates from a custom directory. Copy the default `templates/` directory and modify any template to change the generated manifests — for example, to target a different Kubernetes distribution, add custom annotations, or change the Tekton task structure.

```bash
cp -r templates/ my-templates/
# edit my-templates/dag-pod.yaml.j2 to add custom annotations
python -m src --templates-dir my-templates/ --test-suite examples/minimal/test_suite.yaml --test-lib examples/minimal --cluster cluster/ocp-test.yaml
```

### Custom Aggregation

The `--scripts-dir` CLI arg controls where `aggregate.py` is loaded from. Replace it with a custom script that aggregates into different formats (HTML, database, metrics endpoint) or applies custom filtering:

```bash
cp -r scripts/ my-scripts/
# edit my-scripts/aggregate.py to push results to a dashboard
python -m src --scripts-dir my-scripts/ --test-suite examples/minimal/test_suite.yaml --test-lib examples/minimal --cluster cluster/ocp-test.yaml
```

The script receives the results directory as its first argument and is expected to scan for `junit.xml` files across all step directories.

### Tool Configuration

All images, pod names, labels, and timeouts are configurable via `config.yaml`. Change the Go builder image, use a custom aggregator, adjust timeouts per workload, or change the node selector key for non-standard label schemes:

```yaml
builderImage: my-registry/go-builder:1.25   # custom builder with extra tools
nodeSelectorKey: my.org/node-role            # custom label key
deployTimeout: 1200                           # longer timeout for slow infrastructure
```

### Manifest Validation

`validate_manifest()` in `common.py` checks structural minimums (YAML syntax, required fields). It can be extended with schema validation, dry-run checks, or policy enforcement (image registry allowlists, resource limit requirements) by adding checks to this function.

### Additional Output Layers

The compute/write architecture is designed for extension. Step computation produces a flat list of steps that any writer can consume. A new writer could generate Argo Workflows, GitHub Actions, or Helm charts by reading the same step list and substituting `__TIMESTAMP__` with the appropriate runtime expression.

## Admin Usage

Administrators can create a dedicated test suite with privileged tests for low-level node diagnostics — GPU register checks, PCIe topology inspection, driver-level validation, or anything else that requires host-level access.

### Setting Up a Privileged Test Suite

Create a separate suite directory (e.g. `admin-suite/`) with its own `test_suite.yaml` and test definitions. Set `privileged: true` on DAG steps that need host access:

```yaml
# admin-suite/gpu-diag.yaml
apiVersion: uat.openshift.io/v1
kind: Test
metadata:
  name: gpu-diag
  version: v0.0.1
  description: Low-level GPU diagnostics requiring host access
spec:
  requirements:
    gpu: true
  source:
    ginkgo: gpu-diag.go
  dag:
    - name: diag-runner
      image: nvcr.io/nvidia/cuda:12.8.0-devel-ubi9
      labelFilter: diagnostics
      privileged: true
      env:
        - name: NODE_NAME
          value: '{{ nodeSpec.name }}'
      persistsThroughSweep: false
      parameterSweep: null
```

When `privileged: true` is set, the generated pod spec includes `securityContext.privileged: true` and `hostPID: true`, giving the container full host-level access.

### Generating and Running Manually

Use manual mode to generate manifests for the privileged suite, then run them step by step:

```bash
# Generate only the admin suite
python -m src \
  --test-suite admin-suite/test_suite.yaml \
  --test-lib admin-suite \
  --cluster cluster/ocp-test.yaml \
  --config config.yaml \
  --run-id admin-$(date +%Y%m%d-%H%M%S)

# Run all scripts in order
for f in build/manual/*.sh; do bash "$f"; done
```

Manual mode gives the administrator control over which nodes to target and the ability to inspect results between steps. The privileged pods require a namespace with appropriate SecurityContextConstraints (e.g. `privileged` SCC on OpenShift).

## Project Structure

```
src/
  __main__.py         Entry point
  main.py             CLI parsing, orchestration
  step_generator.py   Setup/teardown step computation, step I/O (steps.json)
  node.py             Node-level step computation, DAG pod rendering
  cluster.py          Cluster-level step computation, placement resolution
  project.py          Project-level step computation (single chain, no node affinity)
  common.py           Jinja2 engine, manifest validation, config loading
  models.py           Pydantic schemas and dataclasses
  writers/
    manual.py         Manual writer (numbered shell scripts + YAML manifests)
    tekton.py         Tekton writer (Tasks, Pipeline, PipelineRun)
tests/                Unit and integration tests
scripts/
  aggregate.py        JUnit XML aggregation (deployed via ConfigMap)
templates/
  *.yaml.j2           Jinja2 templates for Kubernetes/Tekton manifests
  *.sh.j2             Jinja2 templates for shell scripts
examples/
  minimal/            Example test suite (test definitions, Go source)
cluster/              Cluster configs
config.yaml           Tool config
conftest.py           Adds project root to sys.path for pytest
requirements.txt      Python dependencies
```

See [ARCHITECTURE.md](ARCHITECTURE.md) for design decisions and [IMPLEMENTATION.md](IMPLEMENTATION.md) for implementation details.

## Why ConfigMaps

The generator delivers Go source, build scripts, cluster config, test suite config, and the aggregation script to the builder pod via a Kubernetes ConfigMap. The `go.mod` is generated at build time from the Ginkgo version pinned in `config.yaml`, so test authors only need to provide a `.go` source file. An alternative approach — using a setup pod that clones Git repos directly onto the PVC — was explored and rejected due to the following challenges:

- **Cluster config consistency.** The cluster YAML contains sensitive, environment-specific details (node names, GPU counts, namespace, storage config). With Git clones, the cluster config must either live in a public repo (security risk) or on a separate PVC (adding a `clusterConfigSource`/`clusterConfigPvc` configuration surface). The ConfigMap approach uses the same local file the generator already reads, guaranteeing the cluster config in the pod matches what the generator used to compute the DAG.

- **Test suite consistency.** The generator reads test definitions locally (`--test-suite` / `--test-lib`) to compute the step DAG — which pods to create, what commands to run, what sweep entries to generate. A setup pod would clone the test suite from a remote repo at runtime. If the local directory and the remote repo diverge (different branch, uncommitted changes, different path), the DAG won't match the compiled binaries: steps may reference tests that don't exist on the PVC, or miss tests that do. The ConfigMap eliminates this class of drift by bundling exactly the source the generator consumed.

- **Additional infrastructure.** The setup pod approach requires a Python image, network access to clone repos, `pip install` of dependencies, and a standalone `builder.py` script that duplicates test-suite parsing logic already in the generator. The ConfigMap approach has no runtime dependencies beyond `oc` and the Go toolchain.

The ConfigMap approach has a **1MB size limit** (Kubernetes hard constraint). This is sufficient for the current test suite but may become a bottleneck if the number of tests grows significantly. If the limit is hit, the recommended mitigation is to split tests across multiple suite directories and run separate pipelines, or to revisit the Git clone approach with a mechanism to pin the exact commit the generator ran against.
