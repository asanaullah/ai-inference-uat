# Assisted by Claude Opus 4.6
"""Tekton pipeline writer: Tasks, Pipeline, and PipelineRun manifests."""

import shutil
from pathlib import Path

from jinja2 import Environment

from ..common import render_manifest
from ..models import ClusterTestSpec, Step, ToolConfig


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

    ts = "$(params.timestamp)"

    setup_steps = [s for s in steps if s.phase == "setup"]
    test_steps = [s for s in steps if s.phase == "test"]
    teardown_steps = [s for s in steps if s.phase == "teardown"]

    gen_lookup = _build_generate_lookup(steps)

    setup_task_names = _write_tekton_tasks(
        setup_steps,
        gen_lookup,
        tc,
        cs,
        jinja_env,
        tekton_dir,
        timestamp_var=ts,
    )

    test_order = _extract_test_order(test_steps)

    chain_groups: dict[str, list[Step]] = {}
    for s in test_steps:
        chain_groups.setdefault(s.node, []).append(s)

    # Write all test Tekton Task YAMLs and build flat pipeline entries
    test_task_entries: list[dict] = []
    chain_last: dict[tuple[str, str], str] = {}
    chain_first: dict[tuple[str, str], str] = {}
    test_status_tasks: dict[str, list[str]] = {}
    guard_task_names: dict[str, str] = {}

    for test_id, test_name, test_on_failure, test_scope in test_order:
        uses_when = test_on_failure in ("skipTest", "abort")
        test_status_tasks[test_id] = []

        for chain_key, chain_steps in chain_groups.items():
            chain_gen_lookup = _build_generate_lookup(chain_steps)
            chain_gen_lookup.update(gen_lookup)

            t_steps = [s for s in chain_steps if s.test_id == test_id]
            if not t_steps:
                continue

            prev_step: str | None = None
            prev_test_step: str | None = None
            first_set = False

            for step in t_steps:
                if step.type != "command":
                    continue

                task_name = step.resource_name or step.name
                manifest = _resolve_manifest(step, chain_gen_lookup)
                manifest = manifest.replace("__TIMESTAMP__", ts)
                args = [
                    a.replace("__TIMESTAMP__", ts) for a in step.config.get("args", [])
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
                (tekton_dir / f"task-{step.name}.yaml").write_text(task_content)

                entry: dict = {
                    "name": task_name,
                    "ref_name": task_name,
                    "run_after": [prev_step] if prev_step else [],
                    "on_error": "continue",
                }

                if uses_when and prev_test_step and not step.lifecycle:
                    entry["when_expressions"] = [
                        {
                            "input": f"$(tasks.{prev_test_step}.status)",
                            "operator": "in",
                            "values": ["Succeeded"],
                        }
                    ]

                test_task_entries.append(entry)

                if not step.lifecycle:
                    test_status_tasks[test_id].append(task_name)

                if not first_set:
                    chain_first[(test_id, chain_key)] = task_name
                    first_set = True
                prev_step = task_name
                if not step.lifecycle:
                    prev_test_step = task_name

            if prev_step:
                chain_last[(test_id, chain_key)] = prev_step

        # Generate guard task for this test
        guard_name = f"guard-{test_id}-{test_name}"
        guard_on_error = (
            "continue" if test_on_failure in ("continue", "skipTest") else "stopAndFail"
        )
        fan_in = [
            chain_last[(test_id, k)] for k in chain_groups if (test_id, k) in chain_last
        ]

        status_refs = ",".join(
            f"$(tasks.{t}.status)" for t in test_status_tasks[test_id]
        )
        guard_task_content = render_manifest(
            jinja_env,
            "task-guard.yaml.j2",
            {
                "task_name": guard_name,
                "namespace": cs.namespace,
                "managed_by_label": tc.managed_by_label,
                "ose_cli_image": tc.ose_cli_image,
            },
        )
        (tekton_dir / f"task-{guard_name}.yaml").write_text(guard_task_content)

        test_task_entries.append(
            {
                "name": guard_name,
                "ref_name": guard_name,
                "run_after": fan_in,
                "on_error": guard_on_error,
                "params": [{"name": "statuses", "value": status_refs}],
            }
        )
        guard_task_names[test_id] = guard_name

    # Build cluster pipeline — all entries are flat
    cluster_tasks: list[dict] = []
    prev: str | None = None

    # Setup tasks
    for step_name in setup_task_names:
        step = _find_step(setup_steps, step_name, "command")
        if not step:
            continue
        res = step.resource_name or step.name
        cluster_tasks.append(
            {
                "name": res,
                "ref_name": res,
                "run_after": [prev] if prev else [],
                "on_error": "stopAndFail",
            }
        )
        prev = res

    # Link first test tasks to setup (or previous guard)
    prev_guard: str | None = None
    for test_id, test_name, test_on_failure, test_scope in test_order:
        run_after_target = prev_guard if prev_guard else prev

        test_chain_keys = sorted(k for k in chain_groups if (test_id, k) in chain_first)

        if test_scope == "node" and len(test_chain_keys) > 1:
            for chain_key in test_chain_keys:
                first_entry_name = chain_first[(test_id, chain_key)]
                for entry in test_task_entries:
                    if entry["name"] == first_entry_name:
                        if run_after_target:
                            entry["run_after"] = [run_after_target]
                        break
        else:
            prev_chain_last: str | None = None
            for chain_key in test_chain_keys:
                first_entry_name = chain_first[(test_id, chain_key)]
                target = prev_chain_last if prev_chain_last else run_after_target
                for entry in test_task_entries:
                    if entry["name"] == first_entry_name:
                        if target:
                            entry["run_after"] = [target]
                        break
                prev_chain_last = chain_last.get((test_id, chain_key))

        prev_guard = guard_task_names.get(test_id)

    cluster_tasks.extend(test_task_entries)

    # Cluster finally (teardown) — Tekton finally tasks cannot use runAfter,
    # so each namespace's teardown steps are combined into a single Task
    # whose steps execute sequentially.
    ns_groups: dict[str, list[Step]] = {}
    for step in teardown_steps:
        if step.type != "command":
            continue
        ns = step.namespace or cs.namespace
        ns_groups.setdefault(ns, []).append(step)

    cluster_finally: list[dict] = []
    for ns, ns_steps in ns_groups.items():
        composite_name = (
            "finally-teardown" if ns == cs.namespace else f"finally-teardown-{ns}"
        )
        script_steps = []
        for step in ns_steps:
            script = _build_teardown_script(step, gen_lookup, tc, cs, ts)
            step_name = step.resource_name or step.name
            script_steps.append({"name": step_name, "script": script})

        task_content = render_manifest(
            jinja_env,
            "task-finally-sequence.yaml.j2",
            {
                "task_name": composite_name,
                "namespace": ns,
                "managed_by_label": tc.managed_by_label,
                "ose_cli_image": tc.ose_cli_image,
                "steps": script_steps,
            },
        )
        (tekton_dir / f"task-{composite_name}.yaml").write_text(task_content)

        cluster_finally.append(
            {
                "name": composite_name,
                "ref_name": composite_name,
                "on_error": "continue",
            }
        )

    cluster_pipeline = render_manifest(
        jinja_env,
        "pipeline.yaml.j2",
        {
            "pipeline_name": "uat-cluster",
            "namespace": cs.namespace,
            "managed_by_label": tc.managed_by_label,
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
        if (s.name == name or s.resource_name == name) and s.type == step_type:
            return s
    return None


def _build_teardown_script(
    step: Step,
    gen_lookup: dict[str, str],
    tc: ToolConfig,
    cs: ClusterTestSpec,
    timestamp_var: str,
) -> str:
    config = step.config
    cmd = config["command"]
    ns = step.namespace or cs.namespace

    if cmd == "apply":
        manifest = _resolve_manifest(step, gen_lookup)
        manifest = manifest.replace("__TIMESTAMP__", timestamp_var)
        probe = config.get("probe", "none")
        lines = [
            f"oc apply -n {ns} -f - <<'MANIFEST_EOF'",
            manifest,
            "MANIFEST_EOF",
        ]
        if probe == "wait-ready":
            pod_name = config.get("pod_name", "")
            timeout = config.get("timeout", tc.deploy_timeout)
            lines.append(f'echo "Waiting for {pod_name} to be ready..."')
            lines.append(
                f"oc wait --for=condition=Ready pod/{pod_name}"
                f" --timeout={timeout}s -n {ns}"
            )
        return "\n".join(lines)

    if cmd == "exec":
        target = config["target"]
        args = [
            a.replace("__TIMESTAMP__", timestamp_var) for a in config.get("args", [])
        ]
        return f"oc exec {target} -n {ns} -- {' '.join(args)}"

    if cmd == "delete-all":
        configmap = config.get("configmap_name", tc.configmap_name)
        label = tc.managed_by_label
        sel = f"app.kubernetes.io/managed-by={label}"
        return "\n".join(
            [
                'echo "Cleaning up all UAT resources..."',
                f"oc delete pods -l {sel} --ignore-not-found -n {ns}",
                f"oc delete services -l {sel} --ignore-not-found -n {ns}",
                f"oc delete deployments -l {sel} --ignore-not-found -n {ns}",
                f"oc delete configmap {configmap} --ignore-not-found -n {ns}",
                'echo "Cleanup complete"',
            ]
        )

    raise ValueError(f"Unknown teardown command: {cmd}")


def _extract_test_order(
    steps: list[Step],
) -> list[tuple[str, str, str, str]]:
    seen: set[str] = set()
    result: list[tuple[str, str, str, str]] = []
    for step in steps:
        if step.test_id and step.test_id not in seen:
            seen.add(step.test_id)
            result.append((step.test_id, step.test, step.on_failure, step.scope))
    result.sort(key=lambda x: int(x[0].lstrip("t")))
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

        task_name = step.resource_name or step.name
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
        (tekton_dir / f"task-{step.name}.yaml").write_text(task_content)
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

    effective_ns = step.namespace or cs.namespace
    base_ctx = {
        "task_name": task_name,
        "namespace": effective_ns,
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
                "resource_types": config.get(
                    "resource_types", "pods,services,deployments"
                ),
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
