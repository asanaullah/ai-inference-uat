# Minimal Example Test Library

Five test definitions demonstrating the harness's capabilities.

## `component.go` (~556 lines)

Ginkgo test suite validating node hardware and software against the cluster config. Uses `//go:embed cluster.yaml` to access the cluster config at runtime. Env vars: `NODE_NAME`, `RESULTS_DIR`.

**Sanity Checks** (Label `pass-fail`): GPU count via `nvidia-smi -L`, GPU model via `nvidia-smi --query-gpu=name`, NVLink width and all-to-all topology via `nvidia-smi topo -m`, PCIe link width and generation via `nvidia-smi --query-gpu=pcie.link.width.current/pcie.link.gen.current`, CPU model and count from `/proc/cpuinfo`, memory capacity from `/proc/meminfo` (5% tolerance), NUMA node count from `/sys/devices/system/node/node[0-9]*`.

**Ideal Configuration** (Label `pass-fail`): CUDA driver version, GPU power limit (watts), GPU persistence mode, kernel version via `uname -r`, hugepages (2Mi page count from sysfs), CPU frequency governor, CPU idle driver and governor, enabled C-states (iterates sysfs `cpuidle/state*/disable` and `name`), transparent hugepages setting (parses bracketed active value from `/sys/kernel/mm/transparent_hugepage/enabled`).

**Compliance Checks** (Label `pass-fail`): FIPS mode from `/proc/sys/crypto/fips_enabled` (skipped if `compliance.fipsEnabled` is false in cluster config).

## `guidellm.go` (~143 lines)

Ginkgo test for vLLM inference server validation with guidellm benchmarking. Env vars: `SERVER_URL`, `RESULTS_DIR`, `SWEEP_COMMAND` (JSON array).

**Health Check** (Label `pass-fail`): HTTP GET to `<SERVER_URL>/health`, expects 200.

**Model Endpoint** (Label `pass-fail`): HTTP GET to `<SERVER_URL>/v1/models`, verifies response contains non-empty `data` array.

**Benchmark Execution** (Label `quantitative`): parses `SWEEP_COMMAND` as JSON string array, runs the command, checks for result files in `RESULTS_DIR`, parses `benchmarks.json` for `output_tokens_per_second` and `request_latency` metrics (mean, median, min, max). Uses `AddReportEntry` for throughput and latency metrics.

## `inference-perf.go` (~170 lines)

Ginkgo test for vLLM inference benchmarking using [inference-perf](https://github.com/kubernetes-sigs/inference-perf) (kubernetes-sigs). Env vars: `SERVER_URL`, `RESULTS_DIR`, `SWEEP_COMMAND` (JSON array).

**Health Check** (Label `pass-fail`): HTTP GET to `<SERVER_URL>/health`, expects 200.

**Model Endpoint** (Label `pass-fail`): HTTP GET to `<SERVER_URL>/v1/models`, verifies response contains non-empty `data` array.

**Benchmark Execution** (Label `quantitative`): parses `SWEEP_COMMAND` as JSON string array, runs inference-perf, checks for result files in `RESULTS_DIR`, parses `summary_lifecycle_metrics.json` for `successes.count`, `failures.count`, `successes.latency.request_latency`, `successes.throughput.total_tokens_per_sec`, TTFT, and TPOT metrics. Asserts successes > 0, failures == 0. Uses `AddReportEntry` for throughput (total and output tokens/sec), request latency mean, TTFT mean, and TPOT mean.

## `iperf3.go` (~83 lines)

Ginkgo test for TCP bandwidth between cluster nodes. Env vars: `SERVER_HOST`, `RESULTS_DIR`.

**Server Connectivity** (Label `pass-fail`): TCP dial to `<SERVER_HOST>:5201` with 10s timeout.

**Bandwidth Measurement** (Label `pass-fail`): runs `iperf3 -c <host> -t 10 -J`, parses JSON output for `end.sum_received.bits_per_second`, converts to Gbps, asserts > 0. Uses `AddReportEntry` for bandwidth metric.

## `platform-check.go` (~101 lines)

Ginkgo test for platform-level validation using the in-cluster Kubernetes client (`rest.InClusterConfig()`). Env var: `PERMISSION_CHECKS` (JSON array of check objects).

Each check has a `type`, `expected` (`"yes"` or `"no"`), and type-specific fields:
- `type: permission`: runs `SelfSubjectAccessReview` for `verb` + `resource`
- `type: apiGroup`: queries `Discovery().ServerGroups()` for `group` name

Iterates all checks in a single `It` block, collecting failures and reporting pass/fail per check to stdout.
