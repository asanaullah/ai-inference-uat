# Assisted by Claude Opus 4.6
"""CLI parsing, orchestration, setup/teardown step computation, manual writer, Tekton writer."""

import argparse
import re
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
from .cluster import compute_cluster_steps
from .models import ClusterTestSpec, LoadedTest, Step, ToolConfig
from .node import compute_node_steps
from .project import compute_project_steps
from .steps_io import load_steps_file, write_steps_file


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
            all_steps, tc, cs = load_steps_file(Path(args.steps))
        except FileNotFoundError:
            print(f"Error: steps file not found: {args.steps}")
            raise SystemExit(1)
        except Exception as e:
            print(f"Error loading steps file: {e}")
            raise SystemExit(1)
        _validate_unique_pod_names(all_steps)
        _validate_service_names(all_steps)
        print(f"Loaded steps from {args.steps}")
        print(f"Namespace: {cs.namespace}")
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
        print(f"Tests: {[(t.name, t.scope, t.on_failure) for t in suite.spec.tests]}")
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

        test_steps: list[Step] = []
        for test in tests:
            print(f"Processing test: {test.name} (scope: {test.scope})")
            try:
                if test.scope == "node":
                    for node_spec in cs.nodes:
                        steps = compute_node_steps(
                            node_spec,
                            test,
                            tc,
                            cs.namespace,
                            cs.storage.pvc,
                            cs.storage.base_path,
                            jinja_env,
                        )
                        test_steps.extend(steps)
                elif test.scope == "cluster":
                    test_steps.extend(
                        compute_cluster_steps(
                            test,
                            tc,
                            cs.namespace,
                            cs.storage.pvc,
                            cs.storage.base_path,
                            jinja_env,
                            nodes=cs.nodes,
                        )
                    )
                elif test.scope == "project":
                    test_steps.extend(
                        compute_project_steps(
                            test,
                            tc,
                            cs.namespace,
                            cs.storage.pvc,
                            cs.storage.base_path,
                            jinja_env,
                        )
                    )
            except Exception as e:
                print(f"Error computing steps for test {test.name}: {e}")
                raise SystemExit(1)

        try:
            teardown_steps = compute_teardown_steps(tc, cs, jinja_env)
        except Exception as e:
            print(f"Error computing teardown steps: {e}")
            raise SystemExit(1)

        all_steps = setup_steps + test_steps + teardown_steps
        _validate_unique_pod_names(all_steps)
        _validate_service_names(all_steps)
        assign_on_error(all_steps)

        try:
            write_steps_file(all_steps, tc, cs, output_dir / "steps.json")
        except Exception as e:
            print(f"Error writing steps.json: {e}")
            raise SystemExit(1)
        print(f"Steps DAG written to {output_dir / 'steps.json'}")

    # Manual writer
    try:
        write_manual(all_steps, output_dir, args.run_id, jinja_env)
    except Exception as e:
        print(f"Error writing manual output: {e}")
        raise SystemExit(1)

    # Tekton writer
    try:
        write_tekton(all_steps, tc, cs, jinja_env, output_dir)
    except Exception as e:
        print(f"Error writing Tekton output: {e}")
        raise SystemExit(1)

    print(f"\nOutput written to {output_dir}/")


# ---------------------------------------------------------------------------
# Layer 1: Step computation — setup and teardown
# ---------------------------------------------------------------------------


_RFC1123_RE = re.compile(r"^[a-z0-9]([a-z0-9\-]*[a-z0-9])?$")
_DNS1035_RE = re.compile(r"^[a-z]([a-z0-9\-]*[a-z0-9])?$")


def _validate_service_names(steps: list[Step]) -> None:
    for step in steps:
        svc_name = step.config.get("service_name")
        if svc_name and not _DNS1035_RE.match(svc_name):
            raise ValueError(f"Service name '{svc_name}' is not a valid DNS-1035 label")


def _validate_unique_pod_names(steps: list[Step]) -> None:
    pod_names: set[str] = set()
    for step in steps:
        pod_name = step.config.get("pod_name")
        if pod_name:
            if not _RFC1123_RE.match(pod_name):
                raise ValueError(
                    f"Pod name '{pod_name}' is not a valid RFC 1123 subdomain"
                )
            if pod_name in pod_names:
                raise ValueError(f"Duplicate pod name '{pod_name}'")
            pod_names.add(pod_name)


def assign_on_error(steps: list[Step]) -> None:
    for step in steps:
        if step.type != "command":
            continue
        if step.finally_step and step.test:
            continue
        if step.finally_step and not step.test:
            step.config["onError"] = "continue"
            continue
        if step.test and step.on_failure == "continue":
            step.config["onError"] = "continue"
        else:
            step.config["onError"] = "stopAndFail"


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
        {"tests": list(dict.fromkeys(t.name for t in tests))},
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
            name="apply-configmap",
            type="generate",
            config={"output": "manifest"},
            content=cm_content,
            phase="setup",
        )
    )
    steps.append(
        Step(
            name="apply-configmap",
            type="command",
            config={"command": "apply", "probe": "none"},
            source=["apply-configmap"],
            phase="setup",
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
            name="create-builder",
            type="generate",
            config={"output": "manifest"},
            content=builder_content,
            phase="setup",
        )
    )
    steps.append(
        Step(
            name="create-builder",
            type="command",
            config={
                "command": "apply",
                "probe": "wait-ready",
                "pod_name": tc.builder_pod_name,
                "timeout": tc.builder_timeout,
            },
            source=["create-builder"],
            phase="setup",
        )
    )

    steps.append(
        Step(
            name="build",
            type="command",
            config={
                "command": "exec",
                "probe": "none",
                "target": tc.builder_pod_name,
                "args": ["bash", "/src/build.sh"],
            },
            phase="setup",
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
            name="create-aggregator",
            type="generate",
            config={"output": "manifest"},
            content=agg_content,
            finally_step=True,
            phase="teardown",
        )
    )
    steps.append(
        Step(
            name="create-aggregator",
            type="command",
            config={
                "command": "apply",
                "probe": "wait-ready",
                "pod_name": tc.aggregator_pod_name,
                "timeout": tc.aggregator_timeout,
            },
            source=["create-aggregator"],
            finally_step=True,
            phase="teardown",
        )
    )

    steps.append(
        Step(
            name="aggregate",
            type="command",
            config={
                "command": "exec",
                "probe": "none",
                "target": tc.aggregator_pod_name,
                "args": ["python", "/src/aggregate.py", "/workspace"],
            },
            finally_step=True,
            phase="teardown",
        )
    )

    steps.append(
        Step(
            name="cleanup",
            type="command",
            config={
                "command": "delete-all",
                "probe": "none",
                "configmap_name": tc.configmap_name,
                "managed_by_label": tc.managed_by_label,
            },
            finally_step=True,
            phase="teardown",
        )
    )

    return steps


# ---------------------------------------------------------------------------
# Layer 2: Manual writer
# ---------------------------------------------------------------------------


def write_manual(
    steps: list[Step],
    output_dir: Path,
    run_id: str,
    jinja_env: Environment,
) -> None:
    manual_dir = output_dir / "manual"
    if manual_dir.exists():
        shutil.rmtree(manual_dir)
    manual_dir.mkdir(parents=True)

    setup = [s for s in steps if s.phase == "setup"]
    test_steps = [s for s in steps if s.phase == "test"]
    teardown = [s for s in steps if s.phase == "teardown"]

    counter = 1
    for step in setup:
        if _write_step(step, manual_dir, jinja_env, counter):
            counter += 1

    tests_grouped: dict[str, list[Step]] = {}
    for s in test_steps:
        tests_grouped.setdefault(s.test_id, []).append(s)

    for _test_id, t_steps in tests_grouped.items():
        if t_steps[0].scope == "node":
            nodes_grouped: dict[str, list[Step]] = {}
            for s in t_steps:
                nodes_grouped.setdefault(s.node, []).append(s)
            nodes = list(nodes_grouped.keys())
            max_len = max(len(nodes_grouped[n]) for n in nodes)
            for i in range(max_len):
                wrote_any = False
                for n in nodes:
                    if i < len(nodes_grouped[n]):
                        if _write_step(
                            nodes_grouped[n][i], manual_dir, jinja_env, counter
                        ):
                            wrote_any = True
                if wrote_any:
                    counter += 1
        else:
            for s in t_steps:
                if _write_step(s, manual_dir, jinja_env, counter):
                    counter += 1

    for step in teardown:
        if _write_step(step, manual_dir, jinja_env, counter):
            counter += 1

    _stamp(manual_dir, run_id)


def _step_filename(step: Step) -> str:
    return step.name


def _write_step(
    step: Step,
    directory: Path,
    jinja_env: Environment,
    counter: int,
) -> bool:
    assert step.type in ("generate", "command"), f"Unknown step type: {step.type}"

    if step.type == "generate":
        assert "output" in step.config, (
            f"Generate step {step.name} missing config.output"
        )
        assert step.content, f"Generate step {step.name} has empty content"
        filename = _step_filename(step)
        if step.config["output"] == "manifest":
            manifests_dir = directory / "manifests"
            manifests_dir.mkdir(exist_ok=True)
            path = manifests_dir / f"{filename}.yaml"
            path.write_text(step.content)
            return False
        ext = ".sh"
        path = directory / f"{counter}-{filename}{ext}"
        path.write_text(step.content)
        _make_executable(path)
        return True

    script = _derive_manual_script(step, jinja_env)
    if script:
        filename = _step_filename(step)
        path = directory / f"{counter}-{filename}.sh"
        path.write_text(script)
        _make_executable(path)
        return True
    return False


def _derive_manual_script(step: Step, jinja_env: Environment) -> str | None:
    config = step.config
    assert "command" in config, f"Command step {step.name} missing config.command"
    cmd = config["command"]

    if cmd == "apply":
        source_name = step.source[0] if step.source else ""
        manifest = f"manifests/{source_name}.yaml"
        return render_template(
            jinja_env,
            "apply-script.sh.j2",
            {
                "manifest": manifest,
                "probe": config.get("probe", "none"),
                "pod_name": config.get("pod_name", ""),
                "timeout": config.get("timeout", ""),
            },
        )
    elif cmd == "exec":
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
    steps: list[Step],
    tc: ToolConfig,
    cs: ClusterTestSpec,
    jinja_env: Environment,
    output_dir: Path,
) -> None:
    tekton_dir = output_dir / "tekton"
    if tekton_dir.exists():
        shutil.rmtree(tekton_dir)
    tekton_dir.mkdir(parents=True)

    setup_steps = [s for s in steps if s.phase == "setup"]
    test_steps = [s for s in steps if s.phase == "test"]
    teardown_steps = [s for s in steps if s.phase == "teardown"]

    gen_lookup = _build_generate_lookup(steps)

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

    # Derive node→steps grouping and test order from the flat list.
    # Node/test pipelines use $(params.timestamp), NOT $(context.pipelineRun.name),
    # because in Pipeline-in-Pipeline the context resolves to the child
    # PipelineRun name which differs from the parent.
    test_order = _extract_test_order(test_steps)

    non_node_steps = [s for s in test_steps if not s.node]
    if non_node_steps:
        scopes = {s.scope for s in non_node_steps}
        raise ValueError(
            f"Tekton writer does not yet support non-node-scoped test steps "
            f"(found {len(non_node_steps)} steps with scope(s): {scopes})"
        )

    node_groups: dict[str, list[Step]] = {}
    for s in test_steps:
        node_groups.setdefault(s.node, []).append(s)

    for node, n_steps in node_groups.items():
        node_gen_lookup = _build_generate_lookup(n_steps)
        node_gen_lookup.update(gen_lookup)

        test_groups: dict[str, list[Step]] = {}
        for step in n_steps:
            test_groups.setdefault(step.test_id, []).append(step)

        for test_id, test_name, test_on_failure in test_order:
            if test_id not in test_groups:
                continue
            t_steps = test_groups[test_id]

            test_task_entries: list[dict] = []
            test_finally_entries: list[dict] = []
            prev_step: str | None = None

            for step in t_steps:
                if step.type != "command":
                    continue

                task_name = step.name
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
                    "run_after": [prev_step] if prev_step else [],
                    "on_error": step.config.get("onError", "stopAndFail"),
                }

                if step.finally_step:
                    entry["run_after"] = []
                    entry["on_error"] = None
                    test_finally_entries.append(entry)
                else:
                    test_task_entries.append(entry)
                    prev_step = step.name

            test_pipeline_name = f"test-{test_id}-{test_name}-{node}"
            test_pipeline = render_manifest(
                jinja_env,
                "pipeline.yaml.j2",
                {
                    "pipeline_name": test_pipeline_name,
                    "namespace": cs.namespace,
                    "managed_by_label": tc.managed_by_label,
                    "params": [{"name": "timestamp", "type": "string"}],
                    "tasks": test_task_entries,
                    "finally_tasks": test_finally_entries,
                },
            )
            (tekton_dir / f"{test_pipeline_name}.yaml").write_text(test_pipeline)

            outer_on_error = (
                "continue"
                if test_on_failure in ("continue", "skipTest")
                else "stopAndFail"
            )
            node_pipeline_name = f"node-{test_id}-{test_name}-{node}"
            node_pipeline = render_manifest(
                jinja_env,
                "pipeline.yaml.j2",
                {
                    "pipeline_name": node_pipeline_name,
                    "namespace": cs.namespace,
                    "managed_by_label": tc.managed_by_label,
                    "params": [{"name": "timestamp", "type": "string"}],
                    "tasks": [
                        {
                            "name": test_pipeline_name,
                            "ref_type": "pipeline",
                            "ref_name": test_pipeline_name,
                            "params": [
                                {
                                    "name": "timestamp",
                                    "value": "$(params.timestamp)",
                                }
                            ],
                            "run_after": [],
                            "on_error": outer_on_error,
                        }
                    ],
                    "finally_tasks": [],
                },
            )
            (tekton_dir / f"{node_pipeline_name}.yaml").write_text(node_pipeline)

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
                "on_error": "stopAndFail",
            }
        )
        prev = step.name

    prev_entries = [prev] if prev else []
    for test_id, test_name, test_on_failure in test_order:
        on_error = (
            "continue" if test_on_failure in ("continue", "skipTest") else "stopAndFail"
        )
        current_entries = []
        for node in node_groups:
            node_has_test = any(s.test_id == test_id for s in node_groups[node])
            if not node_has_test:
                continue
            entry_name = f"node-{test_id}-{test_name}-{node}"
            cluster_tasks.append(
                {
                    "name": entry_name,
                    "ref_type": "pipeline",
                    "ref_name": entry_name,
                    "params": [
                        {
                            "name": "timestamp",
                            "value": "$(context.pipelineRun.name)",
                        }
                    ],
                    "run_after": list(prev_entries),
                    "on_error": on_error,
                }
            )
            current_entries.append(entry_name)
        if current_entries:
            prev_entries = current_entries

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
                "on_error": step.config.get("onError", "stopAndFail"),
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


def _extract_test_order(
    steps: list[Step],
) -> list[tuple[str, str, str]]:
    seen: set[str] = set()
    result: list[tuple[str, str, str]] = []
    for step in steps:
        if step.test_id and step.test_id not in seen:
            seen.add(step.test_id)
            result.append((step.test_id, step.test, step.on_failure))
    result.sort(key=lambda x: int(x[0]))
    return result


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
                "timeout": config.get("timeout", tc.default_test_timeout),
            },
        )

    if cmd == "exec":
        assert "target" in config, f"Exec step {step.name} missing config.target"
        return render_manifest(
            jinja_env,
            "task-exec.yaml.j2",
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
