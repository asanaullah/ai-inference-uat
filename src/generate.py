# Assisted by Claude Opus 4.6
"""CLI parsing, orchestration, setup/teardown step computation, manual writer, Tekton writer."""

from __future__ import annotations

import argparse
import shutil
import stat
from pathlib import Path

import yaml
from jinja2 import Environment
from pydantic import ValidationError

from .common import (
    create_jinja_env,
    load_config,
    load_tool_config,
    render_manifest,
    render_template,
)
from .models import ClusterTestSpec, LoadedTest, Step, ToolConfig
from .node import compute_node_steps
from .steps_io import load_steps, write_steps


def main() -> None:
    parser = argparse.ArgumentParser(description="UAT Test Harness Generator")
    parser.add_argument("--suite-dir")
    parser.add_argument("--cluster")
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--run-id", default="manual-run")
    parser.add_argument("--output", default="build")
    parser.add_argument("--scripts-dir", default="scripts")
    parser.add_argument("--templates-dir", default="templates")
    parser.add_argument(
        "--steps",
        default=None,
        help="Generate from a steps.json file instead of config",
    )
    args = parser.parse_args()

    output_dir = Path(args.output)

    templates_dir = Path(args.templates_dir)
    if not templates_dir.is_dir():
        print(f"Error: templates directory not found: {args.templates_dir}")
        raise SystemExit(1)
    try:
        jinja_env = create_jinja_env(templates_dir)
    except Exception as e:
        print(f"Error initializing template engine: {e}")
        raise SystemExit(1)

    if args.steps:
        try:
            setup_steps, node_steps, teardown_steps, tc, cs, stop_on_failure = (
                load_steps(Path(args.steps))
            )
        except FileNotFoundError:
            print(f"Error: steps file not found: {args.steps}")
            raise SystemExit(1)
        except Exception as e:
            print(f"Error loading steps file: {e}")
            raise SystemExit(1)
        print(f"Loaded steps from {args.steps}")
        print(f"Namespace: {cs.namespace}")
        print(f"Nodes: {list(node_steps.keys())}")
    else:
        if not args.suite_dir or not args.cluster:
            print(
                "Error: --suite-dir and --cluster are required "
                "when --steps is not provided"
            )
            raise SystemExit(1)

        try:
            tc = load_tool_config(args.config)
        except FileNotFoundError:
            print(f"Error: config file not found: {args.config}")
            raise SystemExit(1)
        except yaml.YAMLError as e:
            print(f"Error: invalid YAML in {args.config}: {e}")
            raise SystemExit(1)
        except ValidationError as e:
            print(f"Error: invalid config in {args.config}:\n{e}")
            raise SystemExit(1)

        try:
            suite, cluster, tests = load_config(args.suite_dir, args.cluster)
        except FileNotFoundError as e:
            print(f"Error: {e}")
            raise SystemExit(1)
        except yaml.YAMLError as e:
            print(f"Error: invalid YAML: {e}")
            raise SystemExit(1)
        except ValidationError as e:
            print(f"Error: invalid test/cluster config:\n{e}")
            raise SystemExit(1)

        cs = cluster.spec

        print(f"Cluster: {Path(args.cluster).stem}")
        print(f"Namespace: {cs.namespace}")
        print(f"Nodes: {[n.name for n in cs.nodes]}")
        print(f"Node tests: {suite.spec.tests.node}")
        if suite.spec.tests.cluster:
            print(f"Cluster tests (not yet implemented): {suite.spec.tests.cluster}")
        if suite.spec.tests.project:
            print(f"Project tests (not yet implemented): {suite.spec.tests.project}")
        print(f"Tests loaded: {[t.name for t in tests]}")

        scripts_dir = Path(args.scripts_dir)
        try:
            aggregate_py = (scripts_dir / "aggregate.py").read_text()
        except FileNotFoundError:
            print(f"Error: aggregate.py not found in {args.scripts_dir}")
            raise SystemExit(1)

        try:
            setup_steps = compute_setup_steps(
                tests,
                tc,
                cs,
                jinja_env,
                args.cluster,
                args.suite_dir,
                aggregate_py,
            )
        except Exception as e:
            print(f"Error computing setup steps: {e}")
            raise SystemExit(1)

        node_steps: dict[str, list[Step]] = {}
        for node_spec in cs.nodes:
            print(f"Processing node: {node_spec.name}")
            try:
                steps = compute_node_steps(
                    node_spec,
                    tests,
                    tc,
                    cs.namespace,
                    cs.storage.pvc,
                    cs.storage.base_path,
                    jinja_env,
                    suite.spec.execution.stop_on_failure,
                )
            except Exception as e:
                print(f"Error computing steps for node {node_spec.name}: {e}")
                raise SystemExit(1)
            if steps:
                node_steps[node_spec.name] = steps
            else:
                print(f"  No tests for {node_spec.name} (all skipped)")

        try:
            teardown_steps = compute_teardown_steps(tc, cs, jinja_env)
        except Exception as e:
            print(f"Error computing teardown steps: {e}")
            raise SystemExit(1)

        stop_on_failure = suite.spec.execution.stop_on_failure

        try:
            write_steps(
                setup_steps,
                node_steps,
                teardown_steps,
                tc,
                cs,
                stop_on_failure,
                output_dir / "steps.json",
            )
        except Exception as e:
            print(f"Error writing steps.json: {e}")
            raise SystemExit(1)
        print(f"Steps DAG written to {output_dir / 'steps.json'}")

    # Manual writer
    try:
        write_manual(
            setup_steps, node_steps, teardown_steps, output_dir, args.run_id, jinja_env
        )
    except Exception as e:
        print(f"Error writing manual output: {e}")
        raise SystemExit(1)

    # Tekton writer
    try:
        write_tekton(
            setup_steps,
            node_steps,
            teardown_steps,
            tc,
            cs,
            jinja_env,
            output_dir,
            stop_on_failure,
        )
    except Exception as e:
        print(f"Error writing Tekton output: {e}")
        raise SystemExit(1)

    print(f"\nOutput written to {output_dir}/")


# ---------------------------------------------------------------------------
# Layer 1: Step computation — setup and teardown
# ---------------------------------------------------------------------------


def compute_setup_steps(
    tests: list[LoadedTest],
    tc: ToolConfig,
    cs: ClusterTestSpec,
    jinja_env: Environment,
    cluster_path: str,
    suite_dir: str,
    aggregate_py: str,
) -> list[Step]:
    assert tests, "No tests loaded"
    assert aggregate_py, "aggregate.py content is empty"

    steps: list[Step] = []

    files: dict[str, str] = {}
    for t in tests:
        assert t.go_source, f"Test {t.name} has empty Go source"
        assert t.go_mod, f"Test {t.name} has empty go.mod"
        files[f"{t.name}_test.go"] = t.go_source
        files[f"{t.name}_go.mod"] = t.go_mod
        files[f"{t.name}_go.sum"] = t.go_sum

    files["cluster.yaml"] = Path(cluster_path).read_text()
    files["test_suite.yaml"] = (Path(suite_dir) / "test_suite.yaml").read_text()
    files["build.sh"] = render_template(
        jinja_env,
        "build.sh.j2",
        {"tests": [t.name for t in tests]},
    )
    files["aggregate.py"] = aggregate_py

    cm_content = render_manifest(
        jinja_env,
        "configmap.yaml.j2",
        {
            "configmap_name": tc.configmap_name,
            "namespace": cs.namespace,
            "managed_by_label": tc.managed_by_label,
            "files": files,
        },
    )
    steps.append(
        Step(
            name="configmap",
            type="generate",
            config={"output": "manifest", "onError": "stop"},
            content=cm_content,
        )
    )
    steps.append(
        Step(
            name="apply-configmap",
            type="command",
            config={"command": "apply", "probe": "none", "onError": "stop"},
            source=["configmap"],
        )
    )

    binaries_subpath = f"{cs.storage.base_path}/__TIMESTAMP__/binaries"
    builder_content = render_manifest(
        jinja_env,
        "support-pod.yaml.j2",
        {
            "pod_name": tc.builder_pod_name,
            "namespace": cs.namespace,
            "managed_by_label": tc.managed_by_label,
            "image": tc.builder_image,
            "pvc": cs.storage.pvc,
            "configmap_name": tc.configmap_name,
            "configmap_mount": True,
            "workspace_subpath": binaries_subpath,
        },
    )
    steps.append(
        Step(
            name="builder-pod",
            type="generate",
            config={"output": "manifest", "onError": "stop"},
            content=builder_content,
        )
    )
    steps.append(
        Step(
            name="create-builder",
            type="command",
            config={
                "command": "apply",
                "probe": "wait-ready",
                "onError": "stop",
                "pod_name": tc.builder_pod_name,
                "timeout": tc.builder_timeout,
            },
            source=["builder-pod"],
        )
    )

    steps.append(
        Step(
            name="build",
            type="command",
            config={
                "command": "exec",
                "probe": "none",
                "onError": "stop",
                "target": tc.builder_pod_name,
                "args": ["bash", "/src/build.sh"],
            },
        )
    )

    return steps


def compute_teardown_steps(
    tc: ToolConfig,
    cs: ClusterTestSpec,
    jinja_env: Environment,
) -> list[Step]:
    steps: list[Step] = []

    timestamp_subpath = f"{cs.storage.base_path}/__TIMESTAMP__"
    agg_content = render_manifest(
        jinja_env,
        "support-pod.yaml.j2",
        {
            "pod_name": tc.aggregator_pod_name,
            "namespace": cs.namespace,
            "managed_by_label": tc.managed_by_label,
            "image": tc.aggregator_image,
            "pvc": cs.storage.pvc,
            "configmap_name": tc.configmap_name,
            "configmap_mount": True,
            "workspace_subpath": timestamp_subpath,
        },
    )
    steps.append(
        Step(
            name="aggregator-pod",
            type="generate",
            config={"output": "manifest", "onError": "run"},
            content=agg_content,
        )
    )
    steps.append(
        Step(
            name="create-aggregator",
            type="command",
            config={
                "command": "apply",
                "probe": "wait-ready",
                "onError": "run",
                "pod_name": tc.aggregator_pod_name,
                "timeout": tc.aggregator_timeout,
            },
            source=["aggregator-pod"],
        )
    )

    steps.append(
        Step(
            name="aggregate",
            type="command",
            config={
                "command": "exec",
                "probe": "none",
                "onError": "run",
                "target": tc.aggregator_pod_name,
                "args": ["python", "/src/aggregate.py", "/workspace"],
            },
        )
    )

    steps.append(
        Step(
            name="cleanup",
            type="command",
            config={
                "command": "delete-all",
                "probe": "none",
                "onError": "run",
                "configmap_name": tc.configmap_name,
                "managed_by_label": tc.managed_by_label,
            },
        )
    )

    return steps


# ---------------------------------------------------------------------------
# Layer 2: Manual writer
# ---------------------------------------------------------------------------


def write_manual(
    setup_steps: list[Step],
    node_steps: dict[str, list[Step]],
    teardown_steps: list[Step],
    output_dir: Path,
    run_id: str,
    jinja_env: Environment,
) -> None:
    manual_dir = output_dir / "manual"
    if manual_dir.exists():
        shutil.rmtree(manual_dir)

    setup_dir = manual_dir / "setup"
    setup_dir.mkdir(parents=True)
    _write_manual_section(setup_steps, setup_dir, jinja_env)

    for node, steps in node_steps.items():
        node_dir = manual_dir / "nodes" / node
        node_dir.mkdir(parents=True)
        _write_manual_numbered(steps, node_dir, jinja_env)

    teardown_dir = manual_dir / "teardown"
    teardown_dir.mkdir(parents=True)
    _write_manual_section(teardown_steps, teardown_dir, jinja_env)

    _stamp(manual_dir, run_id)


def _write_manual_section(
    steps: list[Step],
    directory: Path,
    jinja_env: Environment,
) -> None:
    for step in steps:
        assert step.type in ("generate", "command"), f"Unknown step type: {step.type}"
        if step.type == "generate":
            assert "output" in step.config, (
                f"Generate step {step.name} missing config.output"
            )
            assert step.content, f"Generate step {step.name} has empty content"
            ext = ".yaml" if step.config["output"] == "manifest" else ".sh"
            path = directory / f"{step.name}{ext}"
            path.write_text(step.content)
            if ext == ".sh":
                _make_executable(path)
        elif step.type == "command":
            # Apply commands are handled by the generate step's manifest file;
            # the user runs oc apply -f on it directly.
            if step.config["command"] == "apply":
                continue
            script = _derive_manual_script(step, jinja_env)
            if script:
                path = directory / f"{step.name}.sh"
                path.write_text(script)
                _make_executable(path)


def _write_manual_numbered(
    steps: list[Step],
    directory: Path,
    jinja_env: Environment,
) -> None:
    counter = 1
    for step in steps:
        assert step.type in ("generate", "command"), f"Unknown step type: {step.type}"
        if step.type == "generate":
            assert "output" in step.config, (
                f"Generate step {step.name} missing config.output"
            )
            assert step.content, f"Generate step {step.name} has empty content"
            ext = ".yaml" if step.config["output"] == "manifest" else ".sh"
            path = directory / f"{counter:02d}-{step.name}{ext}"
            path.write_text(step.content)
            if ext == ".sh":
                _make_executable(path)
            counter += 1
        elif step.type == "command":
            # Apply commands are handled by the generate step's manifest file;
            # the user runs oc apply -f on it directly.
            if step.config["command"] == "apply":
                continue
            # onError:'run' steps are Tekton finally-block safety nets;
            # in manual mode the regular teardown step handles cleanup.
            if step.config.get("onError") == "run":
                continue
            script = _derive_manual_script(step, jinja_env)
            if script:
                path = directory / f"{counter:02d}-{step.name}.sh"
                path.write_text(script)
                _make_executable(path)
                counter += 1


def _derive_manual_script(step: Step, jinja_env: Environment) -> str | None:
    config = step.config
    assert "command" in config, f"Command step {step.name} missing config.command"
    cmd = config["command"]

    if cmd == "exec":
        assert "target" in config, f"Exec step {step.name} missing config.target"
        assert "args" in config, f"Exec step {step.name} missing config.args"
        return render_template(
            jinja_env,
            "exec-script.sh.j2",
            {
                "target": config["target"],
                "args": config["args"],
            },
        )
    elif cmd == "delete":
        assert "selector" in config, f"Delete step {step.name} missing config.selector"
        return render_template(
            jinja_env,
            "teardown-script.sh.j2",
            {
                "selector": config["selector"],
            },
        )
    elif cmd == "delete-all":
        return render_template(
            jinja_env,
            "cleanup-script.sh.j2",
            {
                "configmap_name": config.get("configmap_name", ""),
                "managed_by_label": config.get("managed_by_label", ""),
            },
        )
    return None


def _stamp(directory: Path, run_id: str) -> None:
    for path in directory.rglob("*"):
        if path.is_file():
            content = path.read_text()
            if "__TIMESTAMP__" in content:
                path.write_text(content.replace("__TIMESTAMP__", run_id))


def _make_executable(path: Path) -> None:
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


# ---------------------------------------------------------------------------
# Layer 3: Tekton writer
# ---------------------------------------------------------------------------


def write_tekton(
    setup_steps: list[Step],
    node_steps: dict[str, list[Step]],
    teardown_steps: list[Step],
    tc: ToolConfig,
    cs: ClusterTestSpec,
    jinja_env: Environment,
    output_dir: Path,
    stop_on_failure: bool,
) -> None:
    tekton_dir = output_dir / "tekton"
    if tekton_dir.exists():
        shutil.rmtree(tekton_dir)
    tekton_dir.mkdir(parents=True)

    gen_lookup = _build_generate_lookup(
        setup_steps + teardown_steps + [s for sl in node_steps.values() for s in sl]
    )

    # Generate setup Tekton tasks
    setup_task_names = _write_tekton_tasks(
        setup_steps,
        gen_lookup,
        tc,
        cs,
        jinja_env,
        tekton_dir,
        timestamp_var="$(params.timestamp)",
    )

    # Generate node tasks and pipelines.
    # Node pipelines use $(params.timestamp), NOT $(context.pipelineRun.name),
    # because in Pipeline-in-Pipeline the context resolves to the child
    # PipelineRun name which differs from the parent.
    for node, steps in node_steps.items():
        node_gen_lookup = _build_generate_lookup(steps)
        node_gen_lookup.update(gen_lookup)

        node_task_entries: list[dict] = []
        node_finally_entries: list[dict] = []
        prev_name: str | None = None

        for step in steps:
            if step.type != "command":
                continue

            task_name = f"{node}-{step.name}"
            manifest = _resolve_manifest(step, node_gen_lookup)
            manifest = manifest.replace("__TIMESTAMP__", "$(params.timestamp)")
            args = [
                a.replace("__TIMESTAMP__", "$(params.timestamp)")
                for a in step.config.get("args", [])
            ]

            task_content = _render_tekton_task(
                step,
                manifest,
                task_name,
                args,
                tc,
                cs,
                jinja_env,
            )
            (tekton_dir / f"task-{task_name}.yaml").write_text(task_content)

            entry = {
                "name": step.name,
                "ref_type": "task",
                "ref_name": task_name,
                "params": [{"name": "timestamp", "value": "$(params.timestamp)"}],
                "run_after": [prev_name] if prev_name else [],
                "on_error": (
                    "continue" if step.config.get("onError") == "continue" else None
                ),
            }

            if step.config.get("onError") == "run":
                entry["run_after"] = []
                entry["on_error"] = None
                node_finally_entries.append(entry)
            else:
                node_task_entries.append(entry)
                prev_name = step.name

        pipeline_content = render_manifest(
            jinja_env,
            "pipeline.yaml.j2",
            {
                "pipeline_name": f"uat-node-{node}",
                "namespace": cs.namespace,
                "managed_by_label": tc.managed_by_label,
                "params": [{"name": "timestamp", "type": "string"}],
                "tasks": node_task_entries,
                "finally_tasks": node_finally_entries,
            },
        )
        (tekton_dir / f"node-pipeline-{node}.yaml").write_text(pipeline_content)

    # Generate teardown Tekton tasks (same pattern as setup)
    teardown_task_names = _write_tekton_tasks(
        teardown_steps,
        gen_lookup,
        tc,
        cs,
        jinja_env,
        tekton_dir,
        timestamp_var="$(params.timestamp)",
    )

    # Build cluster pipeline
    cluster_tasks: list[dict] = []
    prev: str | None = None

    for step_name in setup_task_names:
        step = _find_step(setup_steps, step_name, "command")
        if not step:
            continue
        cluster_tasks.append(
            {
                "name": step.name,
                "ref_type": "task",
                "ref_name": step.name,
                "params": [
                    {"name": "timestamp", "value": "$(context.pipelineRun.name)"}
                ],
                "run_after": [prev] if prev else [],
                "on_error": None,
            }
        )
        prev = step.name

    for node in node_steps:
        cluster_tasks.append(
            {
                "name": f"run-{node}",
                "ref_type": "pipeline",
                "ref_name": f"uat-node-{node}",
                "params": [
                    {"name": "timestamp", "value": "$(context.pipelineRun.name)"}
                ],
                "run_after": [prev] if prev else [],
                "on_error": None,
            }
        )

    cluster_finally: list[dict] = []
    prev_finally: str | None = None
    for task_name in teardown_task_names:
        step = _find_step(teardown_steps, task_name, "command")
        if not step:
            continue
        cluster_finally.append(
            {
                "name": step.name,
                "ref_type": "task",
                "ref_name": step.name,
                "params": [
                    {"name": "timestamp", "value": "$(context.pipelineRun.name)"}
                ],
                "run_after": [prev_finally] if prev_finally else [],
                "on_error": None,
            }
        )
        prev_finally = step.name

    cluster_pipeline = render_manifest(
        jinja_env,
        "pipeline.yaml.j2",
        {
            "pipeline_name": "uat-cluster",
            "namespace": cs.namespace,
            "managed_by_label": tc.managed_by_label,
            "params": [],
            "tasks": cluster_tasks,
            "finally_tasks": cluster_finally,
        },
    )
    (tekton_dir / "cluster-pipeline.yaml").write_text(cluster_pipeline)

    pipelinerun = render_manifest(
        jinja_env,
        "pipelinerun.yaml.j2",
        {
            "namespace": cs.namespace,
            "managed_by_label": tc.managed_by_label,
            "timeout": tc.pipeline_timeout,
            "finally_timeout": tc.finally_timeout,
        },
    )
    (tekton_dir / "pipelinerun.yaml").write_text(pipelinerun)


def _build_generate_lookup(steps: list[Step]) -> dict[str, str]:
    return {s.name: s.content for s in steps if s.type == "generate"}


def _resolve_manifest(step: Step, lookup: dict[str, str]) -> str:
    if not step.source:
        return ""
    return lookup.get(step.source[0], "")


def _find_step(steps: list[Step], name: str, step_type: str) -> Step | None:
    for s in steps:
        if s.name == name and s.type == step_type:
            return s
    return None


def _write_tekton_tasks(
    steps: list[Step],
    gen_lookup: dict[str, str],
    tc: ToolConfig,
    cs: ClusterTestSpec,
    jinja_env: Environment,
    tekton_dir: Path,
    timestamp_var: str,
) -> list[str]:
    task_names = []
    for step in steps:
        if step.type != "command":
            continue

        task_name = step.name
        manifest = _resolve_manifest(step, gen_lookup)
        manifest = manifest.replace("__TIMESTAMP__", timestamp_var)
        args = [
            a.replace("__TIMESTAMP__", timestamp_var)
            for a in step.config.get("args", [])
        ]

        task_content = _render_tekton_task(
            step,
            manifest,
            task_name,
            args,
            tc,
            cs,
            jinja_env,
        )
        (tekton_dir / f"task-{task_name}.yaml").write_text(task_content)
        task_names.append(task_name)

    return task_names


def _render_tekton_task(
    step: Step,
    manifest: str,
    task_name: str,
    args: list[str],
    tc: ToolConfig,
    cs: ClusterTestSpec,
    jinja_env: Environment,
) -> str:
    config = step.config
    assert "command" in config, f"Step {step.name} missing config.command"
    cmd = config["command"]
    probe = config.get("probe", "none")

    base_ctx = {
        "task_name": task_name,
        "namespace": cs.namespace,
        "managed_by_label": tc.managed_by_label,
        "ose_cli_image": tc.ose_cli_image,
    }

    if cmd == "apply" and probe in ("none", "wait-ready"):
        assert manifest, f"Apply step {step.name} has no manifest to apply"
        return render_manifest(
            jinja_env,
            "task-apply-wait-ready.yaml.j2",
            {
                **base_ctx,
                "manifest": manifest,
                "wait_ready": probe == "wait-ready",
                "pod_name": config.get("pod_name", ""),
                "timeout": config.get("timeout", tc.deploy_timeout),
            },
        )

    if cmd == "apply" and probe == "poll-completed":
        assert manifest, f"Apply step {step.name} has no manifest to apply"
        return render_manifest(
            jinja_env,
            "task-run-test-pod.yaml.j2",
            {
                **base_ctx,
                "manifest": manifest,
                "pod_name": config.get("pod_name", ""),
                "timeout": config.get("timeout", tc.test_timeout),
            },
        )

    if cmd == "exec":
        assert "target" in config, f"Exec step {step.name} missing config.target"
        return render_manifest(
            jinja_env,
            "task-build.yaml.j2",
            {
                **base_ctx,
                "target": config["target"],
                "args": args,
            },
        )

    if cmd == "delete":
        assert "selector" in config, f"Delete step {step.name} missing config.selector"
        return render_manifest(
            jinja_env,
            "task-teardown.yaml.j2",
            {
                **base_ctx,
                "selector": config["selector"],
            },
        )

    if cmd == "delete-all":
        return render_manifest(
            jinja_env,
            "task-cleanup.yaml.j2",
            {
                **base_ctx,
                "configmap_name": config.get("configmap_name", tc.configmap_name),
            },
        )

    raise ValueError(f"Unknown command type: {cmd}")
