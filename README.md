<!-- Assisted by Claude Opus 4.6 -->
# AI Inference UAT Harness
A declarative test harness that generates Kubernetes manifests from test definitions. Given a set of target nodes and a test suite, the generator produces both manually-executable manifests, as well as Tekton pipeline manifests for automated execution on OpenShift.

## Table of Contents

- [How It Works](#how-it-works)
  - [Three-Layer Architecture](#three-layer-architecture)
  - [Intermediate DAG (steps.json)](#intermediate-dag-stepsjson)
  - [Execution Flow](#execution-flow)
  - [Test Scopes](#test-scopes)
  - [PVC Directory Hierarchy](#pvc-directory-hierarchy)
- [Quickstart](#quickstart)
  - [Prerequisites](#prerequisites)
  - [Install](#install)
  - [Generate Manifests](#generate-manifests)
  - [CLI Options](#cli-options)
  - [Run Manually](#run-manually)
  - [Run with Tekton](#run-with-tekton)
- [Adding a Custom Test](#adding-a-custom-test)
- [Test Definition Reference](#test-definition-reference)
  - [DAG Steps](#dag-steps)
  - [Template Variables](#template-variables)
  - [Parameter Sweeps](#parameter-sweeps)
  - [Persistent DAG Pods with Services](#persistent-dag-pods-with-services)
- [Configuration](#configuration)
  - [Cluster Config](#cluster-config-clusternameyaml)
  - [Tool Config](#tool-config-configyaml)
- [Extensibility](#extensibility)
- [Admin Usage](#admin-usage)
- [Project Structure](#project-structure)




## How It Works

The generator reads test definitions (YAML + Go source), a cluster config describing target nodes, and a tool config. It produces two equivalent output formats from the same internal representation:

- **Manual output** (`build/manual/`) — standalone `.yaml` and `.sh` files you run directly with `oc apply -f` and `bash`. Files are organized by phase (setup, per-node, cluster-level, project-level, teardown) and numbered in execution order.

- **Tekton output** (`build/tekton/`) — self-contained Tekton Tasks, Pipelines, and a PipelineRun. Pod manifests are embedded directly in task scripts. Apply the entire directory to run the full suite automatically.

Both outputs are generated from the same ordered step list to ensure they produce identical Kubernetes resources.


```
                                                          +-> Manual Manifests
Test Definitions (YAML + Go) + Node List -> python -m src +                     -> OpenShift Execution -> Results on PVC
                                                          +-> Tekton Manifests
```

### Three-Layer Architecture

1. **Step computation** — converts test definitions into a flat, ordered list of steps. Each step is either a *generate* step (produces a manifest or script) or a *command* step (applies a manifest, execs into a pod, deletes resources).

2. **Manual writer** — writes generate steps as files and derives shell scripts from command steps.

3. **Tekton writer** — derives Tekton Tasks from command steps and assembles them into Pipelines. 

### Intermediate DAG (steps.json)

After step computation and before writing output, the generator serializes the full step list to `build/steps.json`. This file captures the complete DAG — setup, per-node, and teardown steps — along with the tool and cluster config used to produce them.

The generator can also consume `steps.json` as input via `--steps`, skipping config loading and step computation entirely:

```bash
# Normal: compute steps from config, write steps.json + manual + tekton
python -m src --suite-dir test-suite --cluster cluster/ocp-test.yaml

# From steps: load steps.json, write manual + tekton
python -m src --steps build/steps.json
```

This enables a two-phase workflow for custom step injection:

1. Generate `steps.json` from config
2. Edit the file — add, remove, or reorder steps
3. Re-run with `--steps` to produce output from the modified DAG

The file is validated on load with Pydantic (field types, metadata structure) and structural checks (unique step names per section, source references point to existing generate steps, valid command and probe values).

### Execution Flow

```
Setup:    create-builder -> build
Nodes:    [node1-pipeline, node2-pipeline, ...]  (parallel)
Cluster:  cluster-pipeline-with-node-pinning (sequential)
Project:  cluster-pipeline-without-node-pinning (sequential)
Teardown: create-aggregator -> aggregate -> cleanup  (finally block)
```

Each node pipeline runs the full test suite on its target node, pinned via `nodeSelector`:

```
deploy persistent DAG pods (e.g. inference server)
  -> run ephemeral test pod -> cleanup ephemeral pod (per sweep entry)
  -> teardown persistent DAG pods for this test
  -> repeat for next test
```

### Test Scopes

Tests are organized into three scopes based on where and how they run:

- **Node** — validates individual nodes in isolation. Each node-scoped test runs independently on every target node listed in the cluster config, pinned via `nodeSelector`. All node pipelines execute in parallel. Use for hardware validation, GPU diagnostics, driver checks, and single-node inference benchmarks.

- **Cluster** *(future)* — validates behavior that spans multiple nodes but still requires node pinning. Cluster-scoped tests run sequentially with pods pinned to specific nodes. Use for multi-node coordination tests like distributed training, inter-node networking, or GPU-to-GPU communication across nodes.

- **Project** *(future)* — validates namespace-level resources with no node affinity. Project-scoped tests run sequentially without `nodeSelector`, letting the scheduler place pods freely. Use for namespace quota checks, RBAC validation, service mesh configuration, or any test that operates at the project level rather than targeting specific hardware.

Tests are registered under their scope in `test_suite.yaml`:

```yaml
spec:
  tests:
    node:
      - component
      - inference
    cluster: []       # future
    project: []       # future
```

### PVC Directory Hierarchy

Every DAG step gets a unique directory on the PVC, computed transparently by the generator. Test pods write to `/workspace` and files land in the right place via `subPath` mounting.

```
<basePath>/<timestamp>/
  binaries/
    <test_name>/test.bin
  node/
    <node_name>/
      <test_name>/
        <dag_step_name>/
          junit.xml
          ...
  cluster/                  (future)
  project/                  (future)
  report/
    summary.json
```

DAG pods also get a second mount at `/binaries` for access to compiled test binaries.

## Quickstart

### Prerequisites

- Python 3.10+
- An OpenShift cluster with `oc` configured
- A PVC accessible from the target namespace
- Nodes labeled with `kubernetes.io/hostname`

### Install

```bash
pip install -r requirements.txt
```

### Generate Manifests

```bash
python -m src \
  --suite-dir test-suite \
  --cluster cluster/ocp-test.yaml \
  --config config.yaml \
  --templates-dir templates \
  --scripts-dir scripts
```

Output is written to `build/manual/` and `build/tekton/`.

### CLI Options

| Flag | Default | Description |
|---|---|---|
| `--suite-dir` | (required\*) | Directory containing `test_suite.yaml` and test definitions |
| `--cluster` | (required\*) | Path to the cluster config YAML |
| `--config` | `config.yaml` | Path to the tool config |
| `--run-id` | `manual-run` | Timestamp substitute for manual output |
| `--output` | `build` | Output directory |
| `--templates-dir` | `templates` | Path to Jinja2 templates |
| `--scripts-dir` | `scripts` | Path to support scripts (e.g. `aggregate.py`) |
| `--steps` | | Path to a `steps.json` file; skips config loading and step computation |

\* Not required when `--steps` is provided.

### Run Manually

```bash
# Setup
oc apply -f build/manual/setup/configmap.yaml
oc apply -f build/manual/setup/builder-pod.yaml
oc wait --for=condition=Ready pod/ginkgo-builder --timeout=300s
bash build/manual/setup/build.sh

# Run tests on a node (files are numbered to ensure order)
for f in build/manual/nodes/<node_name>/*; do
  case "$f" in
    *.yaml)
      oc apply -f "$f"
      pod=$(grep -m1 'name:' "$f" | awk '{print $2}')
      # Persistent pods (servers): wait for Ready
      # Ephemeral pods (tests): wait for completion
      oc wait --for=condition=Ready pod/"$pod" --timeout=600s 2>/dev/null \
        || oc wait --for=jsonpath='{.status.phase}'=Succeeded pod/"$pod" --timeout=600s
      ;;
    *.sh) bash "$f" ;;
  esac
done

# Teardown
oc apply -f build/manual/teardown/aggregator-pod.yaml
oc wait --for=condition=Ready pod/uat-aggregator --timeout=120s
bash build/manual/teardown/aggregate.sh
bash build/manual/teardown/cleanup.sh
```

### Run with Tekton

```bash
oc apply -f build/tekton/
```

This creates all Tasks, Pipelines, and triggers a PipelineRun. Monitor with:

```bash
oc get pipelineruns -w
```

## Adding a Custom Test

A test requires three files in the suite directory, plus an entry in `test_suite.yaml`.

### 1. Register the Test

Add the test name to `test_suite.yaml` under the appropriate scope:

```yaml
spec:
  tests:
    node:
      - component
      - inference
      - my-test        # add here
```

### 2. Create the Test Definition

Create `test-suite/my-test.yaml`:

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
    goMod: go.mod       # shared across tests in the suite
    goSum: go.sum

  dag:
    - name: test-runner
      type: pod
      image: registry.redhat.io/ubi9/ubi:latest
      labelFilter: pass-fail
      env:
        - name: NODE_NAME
          value: '{{ nodeSpec.name }}'
      persistsThroughSweep: false
      parameterSweep: null
```

### 3. Write the Ginkgo Test

Create `test-suite/my-test.go`:

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
python -m src --suite-dir test-suite --cluster cluster/ocp-test.yaml
```

The generator compiles `my-test.go` into a binary and produces manifests that run it on each target node.

## Test Definition Reference

### DAG Steps

Each test defines an ordered DAG of resources to deploy and run. Steps are either **persistent** (stay up for all sweep entries) or **ephemeral** (run once per sweep entry and exit). Ephemeral pods are cleaned up immediately after completion to release resources (e.g. GPUs) for subsequent steps. Each ephemeral pod carries a `step` label matching its sweep entry ID, enabling targeted deletion without affecting persistent pods. Persistent pods are torn down after all sweep entries complete.

| Field | Description |
|---|---|
| `name` | Step name, used in pod naming and directory hierarchy |
| `image` | Container image |
| `persistsThroughSweep` | `true`: pod stays up (e.g. inference server). `false`: pod runs to completion |
| `labelFilter` | Ginkgo label filter for the compiled binary |
| `command` | Structured command with `args` and `flags` |
| `parameterSweep` | If set, one pod per entry with merged flags |
| `service` | If `enabled: true`, creates a Kubernetes Service for this pod |
| `env` | Environment variables (values are Jinja2 templates) |
| `resources` | CPU/GPU/memory requests and limits |
| `readinessProbe` | Readiness probe for persistent pods |
| `ports` | Container ports for persistent pods |
| `privileged` | If `true`, runs with `securityContext.privileged` and `hostPID` |
| `volumeMounts` | Additional volume mounts (beyond the PVC) |
| `volumes` | Additional volume definitions |

### Template Variables

Available in `command`, `env`, and `resources` values via Jinja2:

| Variable | Description |
|---|---|
| `nodeSpec.*` | Full node spec from cluster config (e.g. `{{ nodeSpec.componentValidation.sanity.gpuCount }}`) |
| `serverConfig.*` | Test-level config dict (e.g. `{{ serverConfig.model }}`) |
| `services["name"].url` | URL of a persistent DAG pod's service |
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

### Persistent DAG Pods with Services

To deploy a long-lived pod (e.g. an inference server) that test pods connect to:

```yaml
dag:
  - name: vllm-server
    image: nvcr.io/nvidia/vllm:26.03-py3
    persistsThroughSweep: true
    service:
      enabled: true
      port: 8000
      name: vllm-server
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

The generator creates a Kubernetes Service alongside the pod. Downstream steps reference it via `{{ services["vllm-server"].url }}`, which resolves to `http://<node>-vllm-server:8000`.

## Configuration

### Cluster Config (`cluster/<name>.yaml`)

Defines target nodes, namespace, storage, and timeout:

```yaml
spec:
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
  timeout: 2h
```

All fields under `componentValidation` are available in Jinja2 templates. The `sanity.gpuCount` field is used for GPU requirement checks — tests with `requirements.gpu: true` are skipped on nodes with `gpuCount: 0`.

### Tool Config (`config.yaml`)

Controls images, pod names, labels, and timeouts:

```yaml
oseCLIImage: registry.redhat.io/openshift4/ose-cli:latest
builderImage: golang:1.25
aggregatorImage: python:3-slim
configmapName: uat-test-source
builderPodName: ginkgo-builder
aggregatorPodName: uat-aggregator
nodeSelectorKey: kubernetes.io/hostname
managedByLabel: uat-generator
builderTimeout: 300s
aggregatorTimeout: 120s
deployTimeout: 600s
testTimeout: 600s
```

## Extensibility

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
python -m src --templates-dir my-templates/ --suite-dir test-suite --cluster cluster/ocp-test.yaml
```

### Custom Aggregation

The `--scripts-dir` CLI arg controls where `aggregate.py` is loaded from. Replace it with a custom script that aggregates into different formats (HTML, database, metrics endpoint) or applies custom filtering:

```bash
cp -r scripts/ my-scripts/
# edit my-scripts/aggregate.py to push results to a dashboard
python -m src --scripts-dir my-scripts/ --suite-dir test-suite --cluster cluster/ocp-test.yaml
```

The script receives the results directory as its first argument and is expected to scan for `junit.xml` files under `node/`, `cluster/`, and `project/` subdirectories.

### Tool Configuration

All images, pod names, labels, and timeouts are configurable via `config.yaml`. Change the Go builder image, use a custom aggregator, adjust timeouts per workload, or change the node selector key for non-standard label schemes:

```yaml
builderImage: my-registry/go-builder:1.25   # custom builder with extra tools
nodeSelectorKey: my.org/node-role            # custom label key
deployTimeout: 1200s                          # longer timeout for slow infrastructure
```

### Manifest Validation

`validate_manifest()` in `common.py` checks structural minimums (YAML syntax, required fields). It can be extended with schema validation, dry-run checks, or policy enforcement (image registry allowlists, resource limit requirements) by adding checks to this function.

### Additional Output Layers

The three-layer architecture (step computation, manual writer, Tekton writer) is designed for extension. The step computation layer produces a flat list of `Step` objects that any output layer can consume. A fourth layer could generate Argo Workflows, GitHub Actions, or Helm charts by reading the same step list and substituting `__TIMESTAMP__` with the appropriate runtime expression.

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
    goMod: go.mod
    goSum: go.sum
  dag:
    - name: diag-runner
      type: pod
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
  --suite-dir admin-suite \
  --cluster cluster/ocp-test.yaml \
  --config config.yaml \
  --run-id admin-$(date +%Y%m%d-%H%M%S)

# Run manually
oc apply -f build/manual/setup/configmap.yaml
oc apply -f build/manual/setup/builder-pod.yaml
oc wait --for=condition=Ready pod/ginkgo-builder --timeout=300s
bash build/manual/setup/build.sh

for f in build/manual/nodes/<node>/*.yaml; do oc apply -f "$f"; done
for f in build/manual/nodes/<node>/*.sh; do bash "$f"; done

bash build/manual/teardown/cleanup.sh
```

Manual mode gives the administrator control over which nodes to target and the ability to inspect results between steps. The privileged pods require a namespace with appropriate SecurityContextConstraints (e.g. `privileged` SCC on OpenShift).

## Project Structure

```
src/
  __main__.py         Entry point
  generate.py         CLI, setup/teardown steps, manual writer, Tekton writer
  node.py             Node-level step computation, DAG pod rendering
  common.py           Jinja2 engine, manifest validation, config loading
  models.py           Pydantic schemas and dataclasses
  steps_io.py         Intermediate DAG serialization and loading
scripts/
  aggregate.py        JUnit XML aggregation (deployed via ConfigMap)
templates/
  *.yaml.j2           Jinja2 templates for Kubernetes/Tekton manifests
  *.sh.j2             Jinja2 templates for shell scripts
test-suite/           Test definitions, Go source, go.mod
cluster/              Cluster configs
config.yaml           Tool config
```

See [ARCHITECTURE.md](ARCHITECTURE.md) for design decisions and [IMPLEMENTATION.md](IMPLEMENTATION.md) for implementation details.
