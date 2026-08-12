#!/usr/bin/env python3
"""
manual_runner.py — Interactive CLI for running UAT test suites manually.

Loads the build directory produced by the UAT generator (steps.json + bash
scripts) and gives the user an interactive shell to inspect, run, and manage
test executions against a live cluster.

Usage:
    python3 scripts/manual_runner.py

===============================================================================
DATA MODEL
===============================================================================

steps.json has two top-level keys:

    metadata:
        toolConfig      — builder/aggregator pod names, images, timeouts,
                           ginkgo version, managed-by label
        clusterSpec     — nodes[] with componentValidation (sanity + ideal),
                           namespace, peerNamespace, storage (pvc, basePath,
                           models.pvc)
        setMappings     — for cluster-scoped tests only. Maps set names to
                           node lists, e.g. {"t3": {"set0": ["wrk-6","wrk-4"],
                           "set1": ["wrk-4","wrk-6"]}}

    steps[]:            — flat list of steps in execution order. Each step has:
        name            — e.g. "t1-platform-check-checker"
        type            — "generate" (emits a manifest) or "command" (runs oc)
        config          — varies by type (see below)
        content         — YAML manifest body (generate steps only)
        source          — list of step names this depends on
        resource_name   — k8s resource name if applicable
        node            — node name for node-scoped, set name for cluster-scoped,
                           empty for project-scoped
        test            — test name, e.g. "platform-check". Empty for global
                           setup/teardown steps.
        test_id         — e.g. "t1", "t2", ..., "t12"
        on_failure      — "abort" or "continue"
        finally_step    — True for teardown steps that must always run
        lifecycle       — True if step manages a persistent pod
        scope           — "project", "node", or "cluster"
        phase           — "setup", "test", or "teardown"
        namespace       — target namespace

    config for "generate" steps:
        {"output": "manifest"}

    config for "command" steps:
        command         — "apply" or "delete"
        probe           — "poll-completed", "wait-ready", or "none"
        pod_name        — pod to wait on (apply steps)
        timeout         — seconds to wait (apply steps)
        selector        — label selector for delete (e.g. "test=guidellm,node=wrk-6")
        resource_types  — comma-separated types for delete (e.g. "pods,services,deployments")

Bash scripts live in <build-dir>/manual/ named NNN-<step-name>.sh. The NNN
prefix is the sequence number. Steps with the same sequence number are
independent and can run in parallel (e.g. node-scoped steps on different
nodes). Each script is self-contained: it calls oc apply/delete, polls for
pod completion, streams logs, and checks exit status.

Global steps (test field is empty):
    Setup:    apply-configmap, create-builder, build (+peer variants)
    Teardown: create-aggregator, aggregate, cleanup (+peer variants)

Per-test steps follow this pattern:
    1. generate + command pairs for each pod (apply manifest, wait for ready/completed)
    2. cleanup steps (delete specific pods after pass-fail or sweep)
    3. teardown steps (delete all resources for the test on that node)
    4. finally-teardown steps (same as teardown but finally_step=True, must always run)

===============================================================================
COMMANDS — DESCRIPTION AND IMPLEMENTATION
===============================================================================

    load <build-dir>
        Load a build directory. Parses steps.json for test metadata, nodes,
        and locates the bash scripts under <build-dir>/manual/. This must be
        run before any other command.

        Implementation:
        - Read <build-dir>/steps.json into memory.
        - Parse metadata.clusterSpec.nodes into a node list.
        - Parse metadata.setMappings for cluster-scoped node resolution.
        - Group steps by test name (steps where test is non-empty). Build a
          dict keyed by test name, each value containing: test_id, scope,
          on_failure, nodes (set of node names from the step's node field),
          and an ordered list of steps.
        - Collect global steps (test is empty) separately, split into
          setup (phase=setup) and teardown (phase=teardown).
        - Scan <build-dir>/manual/*.sh and index them by step name (strip the
          NNN- prefix and .sh suffix). Scripts with the same sequence number
          are grouped as parallel.
        - Validate that every command step in steps.json has a corresponding
          script in manual/. Warn on mismatches.
        - Store build_dir path for later use by save/clear.

    ls tests
        List all tests with index number, name, failure policy, and scope.

        Implementation:
        - Iterate the test dict built during load, sorted by test_id
          (natural sort on the numeric part, e.g. t1 < t2 < t10).
        - Print a table: index (from test_id), name, scope, on_failure,
          nodes (comma-joined or "—" for project-scoped).

    ls tests <test>
        List the bash scripts (steps) within a test, in execution order.
        <test> can be a name or index number.

        Implementation:
        - Resolve <test> to a test name: if numeric, look up by test_id
          (e.g. 1 -> t1); if string, match by name.
        - For the matched test, list its steps in order. For each step show:
          sequence number, script filename, step type (generate/command),
          config.command (apply/delete), config.probe, node, finally_step.
        - Group parallel steps (same sequence number) visually.

    ls nodes
        List the cluster nodes from the loaded build, with GPU count and type.

        Implementation:
        - Iterate metadata.clusterSpec.nodes.
        - For each node, extract: name, nvidia.com/gpu (count),
          resourceNames.nvidia.com/gpu (model name) from
          componentValidation.sanity.
        - Print a table: name, GPU count, GPU model.

    ls nodes <node>
        Show full cluster spec details for a node.

        Implementation:
        - Find the node in metadata.clusterSpec.nodes by name.
        - Pretty-print all of componentValidation.sanity (GPU count, GPU
          memory, CPU count, CPU model, NVLink, PCIe, memory, NUMA) and
          componentValidation.ideal (driver version, power limit,
          persistence mode, kernel, hugepages, CPU governor, idle driver,
          C-states, THP).

    run
        Run all tests in order.

        Implementation:
        - Create a results directory: <build-dir>/results_<YYYYMMDD_HHMMSS>/.
        - Run global setup scripts first (001-apply-configmap.sh through
          the last build step), sequentially.
        - Then run each test's steps in order, sorted by test_id.
        - For steps with the same sequence number (parallel), launch them
          concurrently as subprocesses and wait for all to finish.
        - Each subprocess: run bash script with stdout/stderr piped through
          a tee — stream to terminal AND write to
          <results-dir>/<script-name>.log.
        - After each non-finally step, check exit code. If non-zero and
          on_failure is "abort", skip remaining steps for this test (but
          still run finally-teardown steps), then stop the entire suite.
          If "continue", skip remaining steps for this test (still run
          finally-teardown), then proceed to the next test.
        - Register a signal handler for SIGINT (Ctrl+C). On interrupt:
          kill the running subprocess, run all finally-teardown steps for
          the current test, run global teardown scripts, then exit.

    run <test-indices>
    run -t <test-indices>
        Run specific tests by comma-separated index numbers.

        Implementation:
        - Parse comma-separated indices into a set of test_ids (e.g. "1,3"
          -> {"t1", "t3"}).
        - Filter the test dict to only include those test_ids.
        - Still run global setup scripts first (configmap, builder, build),
          since tests depend on the compiled binaries.
        - Run the filtered tests in test_id order.
        - Still run global teardown at the end.

    run <test-indices> <node-list>
    run -t <test-indices> -n <node-list>
    run -n <node-list>
        Run tests filtered by indices and/or nodes. Flags and positional
        args can be mixed. -n without -t runs all tests on the given nodes.

        Implementation:
        - Parse node-list as comma-separated node names.
        - For node-scoped tests: filter the test's steps to only include
          those whose node field matches a name in the node list. This
          drops per-node script variants for unselected nodes.
        - For cluster-scoped tests: resolve setMappings to find which sets
          contain nodes in the node list. Drop entire set chains whose node
          list has no intersection with the specified nodes.
        - For project-scoped tests: node filter has no effect, run as normal.
        - Rest of execution is the same as "run <test-indices>".

    save
        Collect results from the cluster.

        Implementation:
        - Run the global teardown scripts that create the aggregator pod
          and run aggregate (105-create-aggregator.sh, 106-aggregate.sh).
        - oc cp the results from the aggregator pod's workspace into
          <build-dir>/results_<timestamp>/cluster/.
        - Also copy any locally captured logs from the current run into
          the same results directory.
        - Print the path to the results directory.

    clear
        Reset the cluster workspace.

        Implementation:
        - Run the builder pod scripts (002-create-builder.sh) then exec
          into the builder pod to rm -rf all directories except
          manual-run and cluster.yaml in /uat_workspace.
        - Run the aggregator pod scripts (105-create-aggregator.sh) then
          exec into the aggregator pod to rm -rf the results directory
          on the PVC.
        - Run the cleanup scripts (109-cleanup.sh, 110-peer-cleanup.sh)
          to delete all managed pods/services/deployments.

    exit / quit / Ctrl+D
        Exit the CLI.

        Implementation:
        - Break out of the input loop. If a run is in progress, treat
          as Ctrl+C (trigger finally-teardown, then exit).

===============================================================================
CTRL+C HANDLING
===============================================================================

Signal handler for SIGINT:
    1. Kill the currently running subprocess (if any).
    2. Collect all finally-teardown scripts for the currently running test
       (steps where finally_step=True). Run them sequentially.
    3. Run global teardown scripts (cleanup, peer-cleanup).
    4. Exit with code 130 (standard SIGINT exit code).

The finally_step field in steps.json marks steps that must run even on
failure. These are the delete-all-resources scripts that prevent leaked
pods/services after an interrupted run.

===============================================================================
REUSABLE CODE FROM src/
===============================================================================

models.py:
    Step              — dataclass matching steps.json entries. Use directly
                        to deserialize steps from steps.json.
    StepsFile         — Pydantic model for steps.json with built-in
                        validation (structure, commands, probes, on_failure).
    ToolConfig        — pod names, images, timeouts. Gives us
                        builder_pod_name, aggregator_pod_name, namespace.
    ClusterTestSpec   — nodes[], namespace, peerNamespace, storage.
                        Gives us the node list and cluster details for
                        "ls nodes" and "ls nodes <node>".
    NodeSpec          — name, componentValidation (sanity + ideal).
                        Gives us GPU count, model, NVLink, PCIe, etc.

step_generator.py:
    load_steps_file(path)
                      — Parses steps.json, validates it via StepsFile,
                        returns (list[Step], ToolConfig, ClusterTestSpec).
                        This is the "load" command's core — call it directly
                        instead of reimplementing JSON parsing + validation.

writers/manual.py:
    (reference only)  — Shows how steps map to script filenames. The naming
                        convention is NNN-<step.name>.sh with zero-padded
                        sequence numbers. Node-scoped parallel steps share
                        the same sequence number. Generate steps that output
                        manifests don't get scripts (they write to
                        manifests/<step.name>.yaml instead). This logic is
                        already baked into the generated scripts on disk, so
                        we don't need to rerun it — just index the files.

common.py:
    sanitize_node_name(name)
                      — Converts node names to RFC 1123 DNS labels. Useful
                        if we need to match user-provided node names against
                        step names that embed sanitized node names.
    parse_k8s_quantity(value)
                      — Converts k8s quantity strings (e.g. "1512Gi",
                        "8000Mi") to numeric values. Useful for "ls nodes
                        <node>" pretty-printing.

Not reusable (generation-time only, no value at runtime):
    create_jinja_env, render_template, render_manifest — template rendering
    load_config, load_tool_config — loads from source YAML, not steps.json
    write_manual, write_tekton — writers that produce the build dir
    compute_*_steps — step computation from test definitions
    add_*_steps, _build_render_ctx — step rendering helpers
"""

import json
import re
import signal
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from src.models import ClusterTestSpec, Step, ToolConfig
from src.step_generator import load_steps_file

# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------


@dataclass
class TestInfo:
    test_id: str
    name: str
    scope: str
    on_failure: str
    nodes: list[str]
    steps: list[Step]


@dataclass
class ScriptEntry:
    seq: int
    path: Path
    step_name: str


@dataclass
class State:
    build_dir: Path | None = None
    steps: list[Step] = field(default_factory=list)
    tc: ToolConfig | None = None
    cs: ClusterTestSpec | None = None
    set_mappings: dict[str, dict[str, list[str]]] = field(default_factory=dict)
    tests: dict[str, TestInfo] = field(default_factory=dict)
    global_setup: list[Step] = field(default_factory=list)
    global_teardown: list[Step] = field(default_factory=list)
    scripts: dict[str, list[ScriptEntry]] = field(default_factory=dict)
    results_dir: Path | None = None
    current_procs: list[subprocess.Popen] = field(default_factory=list)
    current_test: TestInfo | None = None


STATE = State()


def _test_id_sort_key(tid: str) -> int:
    m = re.match(r"t(\d+)", tid)
    return int(m.group(1)) if m else 0


# ---------------------------------------------------------------------------
# load
# ---------------------------------------------------------------------------


def cmd_load(args: str) -> None:
    build_dir = Path(args.strip())
    steps_path = build_dir / "steps.json"
    if not steps_path.exists():
        print(f"Error: {steps_path} not found")
        return
    manual_dir = build_dir / "manual"
    if not manual_dir.is_dir():
        print(f"Error: {manual_dir} not found")
        return

    steps, tc, cs = load_steps_file(steps_path)

    with open(steps_path) as f:
        raw = json.load(f)
    set_mappings = raw.get("metadata", {}).get("setMappings", {})

    tests: dict[str, TestInfo] = {}
    global_setup: list[Step] = []
    global_teardown: list[Step] = []

    for s in steps:
        if not s.test:
            if s.phase == "setup":
                global_setup.append(s)
            elif s.phase == "teardown":
                global_teardown.append(s)
            continue
        if s.test not in tests:
            tests[s.test] = TestInfo(
                test_id=s.test_id,
                name=s.test,
                scope=s.scope,
                on_failure=s.on_failure,
                nodes=[],
                steps=[],
            )
        info = tests[s.test]
        info.steps.append(s)
        if s.node and s.node not in info.nodes:
            info.nodes.append(s.node)

    scripts: dict[str, list[ScriptEntry]] = {}
    for f in sorted(manual_dir.glob("*.sh")):
        m = re.match(r"(\d+)-(.+)\.sh$", f.name)
        if not m:
            continue
        seq = int(m.group(1))
        step_name = m.group(2)
        entry = ScriptEntry(seq=seq, path=f, step_name=step_name)
        scripts.setdefault(step_name, []).append(entry)

    STATE.build_dir = build_dir
    STATE.steps = steps
    STATE.tc = tc
    STATE.cs = cs
    STATE.set_mappings = set_mappings
    STATE.tests = tests
    STATE.global_setup = global_setup
    STATE.global_teardown = global_teardown
    STATE.scripts = scripts

    print(
        f"Loaded {len(tests)} tests, {len(cs.nodes)} nodes, "
        f"{sum(len(v) for v in scripts.values())} scripts"
    )


def _require_loaded() -> bool:
    if STATE.build_dir is None:
        print("No build loaded. Run: load <build-dir>")
        return False
    return True


# ---------------------------------------------------------------------------
# ls tests
# ---------------------------------------------------------------------------


def cmd_ls_tests(args: str) -> None:
    if not _require_loaded():
        return
    args = args.strip()
    if args:
        _ls_test_detail(args)
        return

    sorted_tests = sorted(
        STATE.tests.values(), key=lambda t: _test_id_sort_key(t.test_id)
    )
    idx_w = max(len(t.test_id) for t in sorted_tests)
    name_w = max(len(t.name) for t in sorted_tests)
    print(f"  {'#':<{idx_w}}  {'Name':<{name_w}}  {'Scope':<8}  {'On Fail':<10}  Nodes")
    print(f"  {'─' * idx_w}  {'─' * name_w}  {'─' * 8}  {'─' * 10}  {'─' * 20}")
    for t in sorted_tests:
        idx = t.test_id[1:]
        nodes = ",".join(t.nodes) if t.nodes else "—"
        print(
            f"  {idx:<{idx_w}}  {t.name:<{name_w}}  {t.scope:<8}  {t.on_failure:<10}  {nodes}"
        )


def _resolve_test(arg: str) -> TestInfo | None:
    if arg.isdigit():
        tid = f"t{arg}"
        for t in STATE.tests.values():
            if t.test_id == tid:
                return t
        print(f"No test with index {arg}")
        return None
    for t in STATE.tests.values():
        if t.name == arg:
            return t
    print(f"No test named '{arg}'")
    return None


def _ls_test_detail(arg: str) -> None:
    t = _resolve_test(arg)
    if not t:
        return

    print(f"  {t.test_id} {t.name}  scope={t.scope}  on_failure={t.on_failure}")
    print()

    command_steps = [s for s in t.steps if s.type == "command"]
    for s in command_steps:
        entries = STATE.scripts.get(s.name, [])
        if not entries:
            continue
        for e in entries:
            cmd = s.config.get("command", "")
            probe = s.config.get("probe", "")
            finally_mark = " [finally]" if s.finally_step else ""
            node = s.node or "—"
            print(
                f"  {e.seq:03d}  {e.path.name:<55}  {cmd:<7} {probe:<16} node={node}{finally_mark}"
            )


# ---------------------------------------------------------------------------
# ls nodes
# ---------------------------------------------------------------------------


def cmd_ls_nodes(args: str) -> None:
    if not _require_loaded():
        return
    args = args.strip()
    if args:
        _ls_node_detail(args)
        return

    assert STATE.cs is not None
    name_w = max(len(n.name) for n in STATE.cs.nodes)
    print(f"  {'Name':<{name_w}}  {'GPUs':>4}  Model")
    print(f"  {'─' * name_w}  {'─' * 4}  {'─' * 30}")
    for n in STATE.cs.nodes:
        sanity = n.component_validation.sanity.model_dump()
        gpu_count = sanity.get("nvidia.com/gpu", "?")
        gpu_model = sanity.get("resourceNames", {}).get("nvidia.com/gpu", "?")
        print(f"  {n.name:<{name_w}}  {gpu_count:>4}  {gpu_model}")


def _ls_node_detail(name: str) -> None:
    assert STATE.cs is not None
    node = None
    for n in STATE.cs.nodes:
        if n.name == name:
            node = n
            break
    if not node:
        print(f"No node named '{name}'")
        return

    cv = node.component_validation.model_dump()
    sanity = cv.get("sanity", {})
    ideal = cv.get("ideal", {})

    print(f"  Node: {node.name}")
    print()
    if sanity:
        print("  Sanity:")
        _print_dict(sanity, indent=4)
    if ideal:
        print()
        print("  Ideal:")
        _print_dict(ideal, indent=4)


def _print_dict(d: dict, indent: int = 0) -> None:
    pad = " " * indent
    for k, v in d.items():
        if isinstance(v, dict):
            print(f"{pad}{k}:")
            _print_dict(v, indent + 2)
        elif isinstance(v, list):
            print(f"{pad}{k}: {v}")
        else:
            print(f"{pad}{k}: {v}")


# ---------------------------------------------------------------------------
# run
# ---------------------------------------------------------------------------


def _find_scripts(step: Step) -> list[ScriptEntry]:
    return STATE.scripts.get(step.name, [])


def _run_script(entry: ScriptEntry, results_dir: Path) -> int:
    log_path = results_dir / f"{entry.path.stem}.log"
    print(f"\n{'=' * 60}")
    print(f"  Running: {entry.path.name}")
    print(f"{'=' * 60}\n")

    proc = subprocess.Popen(
        ["bash", str(entry.path)],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        cwd=str(entry.path.parent),
    )
    STATE.current_procs.append(proc)

    with open(log_path, "w") as log_file:
        assert proc.stdout is not None
        for line in iter(proc.stdout.readline, b""):
            text = line.decode("utf-8", errors="replace")
            sys.stdout.write(text)
            sys.stdout.flush()
            log_file.write(text)

    proc.wait()
    if proc in STATE.current_procs:
        STATE.current_procs.remove(proc)
    return proc.returncode


def _run_parallel(entries: list[ScriptEntry], results_dir: Path) -> dict[str, int]:
    results: dict[str, int] = {}
    if len(entries) == 1:
        rc = _run_script(entries[0], results_dir)
        results[entries[0].step_name] = rc
        return results

    procs: list[tuple[ScriptEntry, subprocess.Popen, Path]] = []
    for entry in entries:
        log_path = results_dir / f"{entry.path.stem}.log"
        print(f"  Starting: {entry.path.name}")
        log_file = open(log_path, "w")  # noqa: SIM115
        proc = subprocess.Popen(
            ["bash", str(entry.path)],
            stdout=log_file,
            stderr=subprocess.STDOUT,
            cwd=str(entry.path.parent),
        )
        STATE.current_procs.append(proc)
        procs.append((entry, proc, log_path))

    for entry, proc, log_path in procs:
        proc.wait()
        if proc in STATE.current_procs:
            STATE.current_procs.remove(proc)
        results[entry.step_name] = proc.returncode
        status = "OK" if proc.returncode == 0 else f"FAILED ({proc.returncode})"
        print(f"  Finished: {entry.path.name} — {status}")

    return results


def _run_finally_steps(test_info: TestInfo, results_dir: Path) -> None:
    finally_steps = [
        s for s in test_info.steps if s.finally_step and s.type == "command"
    ]
    for s in finally_steps:
        entries = _find_scripts(s)
        for e in entries:
            _run_script(e, results_dir)


def _run_global_teardown(results_dir: Path) -> None:
    for s in STATE.global_teardown:
        if s.type != "command":
            continue
        entries = _find_scripts(s)
        for e in entries:
            _run_script(e, results_dir)


def _filter_steps_by_nodes(
    test_info: TestInfo, node_filter: set[str] | None
) -> list[Step]:
    if node_filter is None:
        return test_info.steps

    if test_info.scope == "project":
        return test_info.steps

    if test_info.scope == "node":
        return [s for s in test_info.steps if not s.node or s.node in node_filter]

    if test_info.scope == "cluster":
        tid = test_info.test_id
        mappings = STATE.set_mappings.get(tid, {})
        allowed_sets: set[str] = set()
        for set_name, set_nodes in mappings.items():
            if node_filter.intersection(set_nodes):
                allowed_sets.add(set_name)
        return [s for s in test_info.steps if not s.node or s.node in allowed_sets]

    return test_info.steps


def _parse_run_args(args: str) -> tuple[set[str] | None, set[str] | None]:
    test_filter: set[str] | None = None
    node_filter: set[str] | None = None
    tokens = args.strip().split()

    i = 0
    positional: list[str] = []
    while i < len(tokens):
        if tokens[i] == "-t" and i + 1 < len(tokens):
            test_filter = {f"t{x.strip()}" for x in tokens[i + 1].split(",")}
            i += 2
        elif tokens[i] == "-n" and i + 1 < len(tokens):
            node_filter = {x.strip() for x in tokens[i + 1].split(",")}
            i += 2
        else:
            positional.append(tokens[i])
            i += 1

    if positional and test_filter is None:
        test_filter = {f"t{x.strip()}" for x in positional[0].split(",")}
    if len(positional) >= 2 and node_filter is None:
        node_filter = {x.strip() for x in positional[1].split(",")}

    return test_filter, node_filter


def cmd_run(args: str) -> None:
    if not _require_loaded():
        return

    test_filter, node_filter = _parse_run_args(args)

    sorted_tests = sorted(
        STATE.tests.values(), key=lambda t: _test_id_sort_key(t.test_id)
    )
    if test_filter:
        sorted_tests = [t for t in sorted_tests if t.test_id in test_filter]
        if not sorted_tests:
            print("No matching tests found")
            return

    ts = datetime.now(tz=timezone.utc).strftime("%Y%m%d_%H%M%S")
    assert STATE.build_dir is not None
    results_dir = STATE.build_dir / f"results_{ts}"
    results_dir.mkdir(parents=True, exist_ok=True)
    STATE.results_dir = results_dir
    print(f"Results directory: {results_dir}")

    abort_all = False

    # Global setup
    print(f"\n{'#' * 60}")
    print("  Global Setup")
    print(f"{'#' * 60}")
    for s in STATE.global_setup:
        if s.type != "command":
            continue
        entries = _find_scripts(s)
        for e in entries:
            rc = _run_script(e, results_dir)
            if rc != 0:
                print(f"Global setup failed: {e.path.name}")
                return

    # Tests
    for test_info in sorted_tests:
        STATE.current_test = test_info
        filtered_steps = _filter_steps_by_nodes(test_info, node_filter)
        command_steps = [s for s in filtered_steps if s.type == "command"]
        if not command_steps:
            continue

        print(f"\n{'#' * 60}")
        print(f"  Test: {test_info.test_id} {test_info.name} ({test_info.scope})")
        print(f"{'#' * 60}")

        test_failed = False
        seq_groups: list[tuple[int, list[tuple[Step, ScriptEntry]]]] = []
        for s in command_steps:
            if s.finally_step:
                continue
            entries = _find_scripts(s)
            for e in entries:
                if seq_groups and seq_groups[-1][0] == e.seq:
                    seq_groups[-1][1].append((s, e))
                else:
                    seq_groups.append((e.seq, [(s, e)]))

        for seq, group in seq_groups:
            if test_failed:
                break
            entries = [e for _, e in group]
            if len(entries) == 1:
                _step, entry = group[0]
                rc = _run_script(entry, results_dir)
                if rc != 0:
                    test_failed = True
                    print(f"  Step failed: {entry.path.name} (rc={rc})")
            else:
                results = _run_parallel(entries, results_dir)
                if any(rc != 0 for rc in results.values()):
                    test_failed = True

        _run_finally_steps(test_info, results_dir)
        STATE.current_test = None

        if test_failed and test_info.on_failure == "abort":
            print(f"\n  Test {test_info.name} failed with on_failure=abort, stopping.")
            abort_all = True
            break
        elif test_failed:
            print(f"\n  Test {test_info.name} failed, continuing.")

    if not abort_all:
        print(f"\n{'#' * 60}")
        print("  Global Teardown")
        print(f"{'#' * 60}")
        _run_global_teardown(results_dir)

    print(f"\nResults saved to: {results_dir}")


# ---------------------------------------------------------------------------
# save
# ---------------------------------------------------------------------------


def cmd_save(args: str) -> None:
    if not _require_loaded():
        return
    assert STATE.build_dir is not None and STATE.tc is not None and STATE.cs is not None

    ts = datetime.now(tz=timezone.utc).strftime("%Y%m%d_%H%M%S")
    results_dir = STATE.build_dir / f"results_{ts}"
    results_dir.mkdir(parents=True, exist_ok=True)

    # Run aggregator scripts
    for s in STATE.global_teardown:
        if s.type != "command":
            continue
        if "aggregator" in s.name or "aggregate" in s.name:
            entries = _find_scripts(s)
            for e in entries:
                _run_script(e, results_dir)

    # Copy results from aggregator pod
    ns = STATE.cs.namespace
    pod = STATE.tc.aggregator_pod_name
    base = STATE.cs.storage.base_path
    local_cluster = results_dir / "cluster"
    local_cluster.mkdir(exist_ok=True)

    print(f"Copying results from {pod}:/uat_workspace/{base}/ ...")
    subprocess.run(
        ["oc", "cp", f"{pod}:/uat_workspace/{base}/", str(local_cluster), "-n", ns],
        check=False,
    )
    print(f"Results saved to: {results_dir}")


# ---------------------------------------------------------------------------
# clear
# ---------------------------------------------------------------------------


def cmd_clear(args: str) -> None:
    if not _require_loaded():
        return
    assert STATE.tc is not None and STATE.cs is not None

    ns = STATE.cs.namespace
    builder = STATE.tc.builder_pod_name
    aggregator = STATE.tc.aggregator_pod_name
    base = STATE.cs.storage.base_path

    # Ensure builder pod exists
    for s in STATE.global_setup:
        if s.type == "command" and "create-builder" in s.name:
            entries = _find_scripts(s)
            for e in entries:
                subprocess.run(
                    ["bash", str(e.path)], cwd=str(e.path.parent), check=False
                )
            break

    # Clear builder workspace
    print("Clearing builder workspace...")
    subprocess.run(
        [
            "oc",
            "exec",
            builder,
            "-n",
            ns,
            "--",
            "bash",
            "-c",
            (
                "for d in /uat_workspace/*/; do "
                'name=$(basename "$d"); '
                'case "$name" in manual-run|cluster.yaml) ;; *) rm -rf "$d";; esac; done'
            ),
        ],
        check=False,
    )

    # Ensure aggregator pod exists
    for s in STATE.global_teardown:
        if (
            s.type == "command"
            and "create-aggregator" in s.name
            and "peer" not in s.name
        ):
            entries = _find_scripts(s)
            for e in entries:
                subprocess.run(
                    ["bash", str(e.path)], cwd=str(e.path.parent), check=False
                )
            break

    # Clear results
    print("Clearing results...")
    subprocess.run(
        [
            "oc",
            "exec",
            aggregator,
            "-n",
            ns,
            "--",
            "bash",
            "-c",
            f"rm -rf /uat_workspace/{base}/*",
        ],
        check=False,
    )

    # Run cleanup scripts
    for s in STATE.global_teardown:
        if s.type == "command" and "cleanup" in s.name:
            entries = _find_scripts(s)
            for e in entries:
                print(f"Running {e.path.name}...")
                subprocess.run(
                    ["bash", str(e.path)], cwd=str(e.path.parent), check=False
                )

    print("Cleared.")


# ---------------------------------------------------------------------------
# Signal handling
# ---------------------------------------------------------------------------


def _sigint_handler(signum: int, frame: Any) -> None:
    print("\n\nInterrupted. Running teardown...")

    for proc in STATE.current_procs:
        try:
            proc.kill()
        except OSError:
            pass
    STATE.current_procs.clear()

    if STATE.current_test and STATE.results_dir:
        _run_finally_steps(STATE.current_test, STATE.results_dir)

    if STATE.results_dir:
        _run_global_teardown(STATE.results_dir)

    sys.exit(130)


# ---------------------------------------------------------------------------
# REPL
# ---------------------------------------------------------------------------


COMMANDS: dict[str, Any] = {
    "load": cmd_load,
    "ls tests": cmd_ls_tests,
    "ls nodes": cmd_ls_nodes,
    "run": cmd_run,
    "save": cmd_save,
    "clear": cmd_clear,
}


def _dispatch(line: str) -> bool:
    line = line.strip()
    if not line:
        return True
    if line in ("exit", "quit"):
        return False

    for prefix in sorted(COMMANDS.keys(), key=len, reverse=True):
        if line == prefix or line.startswith(prefix + " "):
            rest = line[len(prefix) :].strip()
            COMMANDS[prefix](rest)
            return True

    print(f"Unknown command: {line.split()[0]}")
    print("Commands: load, ls tests, ls nodes, run, save, clear, exit")
    return True


def main() -> None:
    signal.signal(signal.SIGINT, _sigint_handler)

    print("UAT Manual Runner")
    print("Type 'load <build-dir>' to start, 'exit' to quit.\n")

    while True:
        try:
            line = input("uat> ")
        except EOFError:
            print()
            break
        if not _dispatch(line):
            break


if __name__ == "__main__":
    main()
