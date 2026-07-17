# Assisted by Claude Opus 4.6
"""Node-level step computation, DAG/test pod rendering, and requirement checks."""

from typing import Any

from jinja2 import Environment

from .common import build_command, render_manifest, render_string
from .models import DAGStep, LoadedTest, NodeSpec, Step, ToolConfig


def node_meets_requirements(requirements: Any, node_spec: NodeSpec) -> bool:
    if requirements.gpu and node_spec.component_validation.sanity.gpu_count <= 0:
        return False
    return True


def compute_node_steps(
    node_spec: NodeSpec,
    test: LoadedTest,
    tool_config: ToolConfig,
    namespace: str,
    pvc: str,
    base_path: str,
    jinja_env: Environment,
) -> list[Step]:
    # Called once per node — the DAG traversal and template rendering are
    # identical each time, but the output differs: node name is baked into pod
    # names, labels, selectors, and PVC subpaths, producing distinct manifests
    # that can't be shared across nodes.
    steps: list[Step] = []
    node = node_spec.name
    safe_node = node_spec.sanitized_name or node
    node_spec_dict = node_spec.model_dump(by_alias=True)

    if not node_meets_requirements(test.spec.requirements, node_spec):
        print(f"  Skipping {test.name} on {node} (requirements not met)")
        return steps

    services: dict[str, dict[str, Any]] = {}
    has_persistent = False

    for dag_step in test.spec.dag:
        if dag_step.persists_through_sweep:
            if dag_step.parameter_sweep is not None:
                raise ValueError(
                    f"DAG step '{dag_step.name}' in test '{test.name}' has "
                    f"both persistsThroughSweep and parameterSweep set — "
                    f"parameterSweep is only valid on non-persistent steps"
                )
            has_persistent = True
            _add_persistent_steps(
                steps,
                dag_step,
                node,
                safe_node,
                test,
                tool_config,
                namespace,
                pvc,
                base_path,
                node_spec_dict,
                services,
                jinja_env,
            )
        else:
            _add_ephemeral_steps(
                steps,
                dag_step,
                node,
                safe_node,
                test,
                tool_config,
                namespace,
                pvc,
                base_path,
                node_spec_dict,
                services,
                jinja_env,
            )

    step_prefix = f"{test.test_id}-{test.name}-{node}"
    res_prefix = f"{test.test_id}-{test.name}-{safe_node}"
    selector = f"test={test.name},node={node}"
    if has_persistent:
        steps.append(
            Step(
                name=f"{step_prefix}-teardown",
                type="command",
                config={
                    "command": "delete",
                    "probe": "none",
                    "selector": selector,
                },
                resource_name=f"{res_prefix}-teardown",
                node=node,
                test=test.name,
                test_id=test.test_id,
                on_failure=test.on_failure,
                scope="node",
                phase="test",
            )
        )
    steps.append(
        Step(
            name=f"{step_prefix}-finally-teardown",
            type="command",
            config={
                "command": "delete",
                "probe": "none",
                "selector": selector,
            },
            resource_name=f"{res_prefix}-finally-teardown",
            node=node,
            test=test.name,
            test_id=test.test_id,
            on_failure=test.on_failure,
            finally_step=True,
            scope="node",
            phase="test",
        )
    )

    return steps


# Node-specific helpers: these use nodeSelector, node-prefixed pod names, and
# node-scoped labels. Kept here because cluster/project scopes are not yet
# implemented, so the right abstraction boundary is unknown.


def _add_persistent_steps(
    steps: list[Step],
    dag_step: DAGStep,
    node: str,
    safe_node: str,
    test: LoadedTest,
    tc: ToolConfig,
    namespace: str,
    pvc: str,
    base_path: str,
    node_spec_dict: dict,
    services: dict,
    jinja_env: Environment,
) -> None:
    step_prefix = f"{test.test_id}-{test.name}-{node}"
    res_prefix = f"{test.test_id}-{test.name}-{safe_node}"
    step_name = f"{step_prefix}-{dag_step.name}"
    pod_name = f"{res_prefix}-{dag_step.name}"

    render_ctx = {
        "timestamp": "__TIMESTAMP__",
        "node": node,
        "serverConfig": test.spec.server_config,
        "services": services,
        "nodeSpec": node_spec_dict,
    }

    command = None
    if dag_step.command:
        cmd_list = build_command(dag_step.command.args, dag_step.command.flags)
        cmd_list = [render_string(jinja_env, str(c), render_ctx) for c in cmd_list]
        command = cmd_list

    env = _render_env(dag_step.env, render_ctx, jinja_env)
    resources = (
        _render_resources(dag_step.resources, render_ctx, jinja_env)
        if dag_step.resources
        else None
    )

    if dag_step.service.enabled:
        svc_name = f"svc-{res_prefix}-{dag_step.service.name}"
        services[dag_step.service.name] = {
            "url": f"http://{svc_name}:{dag_step.service.port}",
            "name": svc_name,
            "port": dag_step.service.port,
        }

    workspace_subpath = f"{base_path}/__TIMESTAMP__/{step_name}"
    binaries_subpath = f"{base_path}/__TIMESTAMP__/binaries"

    pod_ctx = {
        "pod_name": pod_name,
        "namespace": namespace,
        "managed_by_label": tc.managed_by_label,
        "test": test.name,
        "node": node,
        "dag_step_name": dag_step.name,
        "node_selector_key": tc.node_selector_key,
        "image": dag_step.image,
        "command": command,
        "env": env,
        "ports": dag_step.ports,
        "readiness_probe": dag_step.readiness_probe,
        "resources": resources,
        "volume_mounts": dag_step.volume_mounts,
        "volumes": dag_step.volumes,
        "pvc": pvc,
        "privileged": dag_step.privileged,
        "workspace_subpath": workspace_subpath,
        "binaries_subpath": binaries_subpath,
    }
    content = render_manifest(jinja_env, "dag-pod.yaml.j2", pod_ctx)

    if dag_step.service.enabled:
        svc_ctx = {
            "service_name": services[dag_step.service.name]["name"],
            "pod_name": pod_name,
            "port": dag_step.service.port,
            "namespace": namespace,
            "node": node,
            "test": test.name,
            "dag_step_name": dag_step.name,
            "managed_by_label": tc.managed_by_label,
            "headless": dag_step.service.headless,
        }
        svc_content = render_manifest(jinja_env, "dag-service.yaml.j2", svc_ctx)
        content = content + "\n---\n" + svc_content

    gen_config = {"output": "manifest"}
    if dag_step.service.enabled:
        gen_config["service_name"] = svc_name

    steps.append(
        Step(
            name=step_name,
            type="generate",
            config=gen_config,
            content=content,
            resource_name=pod_name,
            node=node,
            test=test.name,
            test_id=test.test_id,
            on_failure=test.on_failure,
            scope="node",
            phase="test",
        )
    )
    steps.append(
        Step(
            name=step_name,
            type="command",
            config={
                "command": "apply",
                "probe": "wait-ready",
                "pod_name": pod_name,
                "timeout": tc.deploy_timeout,
            },
            source=[step_name],
            resource_name=pod_name,
            node=node,
            test=test.name,
            test_id=test.test_id,
            on_failure=test.on_failure,
            scope="node",
            phase="test",
        )
    )


def _add_ephemeral_steps(
    steps: list[Step],
    dag_step: DAGStep,
    node: str,
    safe_node: str,
    test: LoadedTest,
    tc: ToolConfig,
    namespace: str,
    pvc: str,
    base_path: str,
    node_spec_dict: dict,
    services: dict,
    jinja_env: Environment,
) -> None:
    step_prefix = f"{test.test_id}-{test.name}-{node}"
    res_prefix = f"{test.test_id}-{test.name}-{safe_node}"
    has_sweep = dag_step.parameter_sweep is not None

    svc_name = ""
    if dag_step.service.enabled:
        svc_name = f"svc-{res_prefix}-{dag_step.service.name}"
        services[dag_step.service.name] = {
            "url": f"http://{svc_name}:{dag_step.service.port}",
            "name": svc_name,
            "port": dag_step.service.port,
        }

    if has_sweep:
        entries = [
            (
                e.id,
                e.description,
                {**dag_step.parameter_sweep.base_command.flags, **e.flags},
            )
            for e in dag_step.parameter_sweep.entries
        ]
    else:
        entries = [(dag_step.name, "", {})]

    for sweep_id, _sweep_desc, sweep_flags in entries:
        if has_sweep:
            step_name = f"{step_prefix}-{dag_step.name}-{sweep_id}"
            res_name = f"{res_prefix}-{dag_step.name}-{sweep_id}"
            cleanup_name = f"{step_prefix}-cleanup-{dag_step.name}-{sweep_id}"
            cleanup_res = f"{res_prefix}-cleanup-{dag_step.name}-{sweep_id}"
        else:
            step_name = f"{step_prefix}-{dag_step.name}"
            res_name = f"{res_prefix}-{dag_step.name}"
            cleanup_name = f"{step_prefix}-cleanup-{dag_step.name}"
            cleanup_res = f"{res_prefix}-cleanup-{dag_step.name}"

        pod_name = res_name
        param_sweep: dict[str, Any] = {"id": sweep_id}
        workspace_subpath = f"{base_path}/__TIMESTAMP__/{step_name}"
        binaries_subpath = f"{base_path}/__TIMESTAMP__/binaries"

        render_ctx: dict[str, Any] = {
            "timestamp": "__TIMESTAMP__",
            "node": node,
            "serverConfig": test.spec.server_config,
            "services": services,
            "nodeSpec": node_spec_dict,
        }

        if has_sweep:
            args = dag_step.parameter_sweep.base_command.args
            sweep_cmd = build_command(args, sweep_flags)
            sweep_cmd = [
                render_string(jinja_env, str(v), render_ctx) for v in sweep_cmd
            ]
            param_sweep["command"] = sweep_cmd

        full_ctx = {**render_ctx, "paramSweep": param_sweep}

        if dag_step.label_filter:
            binary = f"/binaries/{test.name}/test.bin"
            pod_command = [
                binary,
                f"--ginkgo.label-filter={dag_step.label_filter}",
                "--ginkgo.junit-report=/workspace/junit.xml",
            ]
        elif dag_step.command:
            if has_sweep:
                pod_command = build_command(
                    dag_step.parameter_sweep.base_command.args,
                    sweep_flags,
                )
            else:
                pod_command = build_command(
                    dag_step.command.args, dag_step.command.flags
                )
            pod_command = [
                render_string(jinja_env, str(v), full_ctx) for v in pod_command
            ]
        else:
            pod_command = None

        env = _render_env(list(dag_step.env), full_ctx, jinja_env)

        if dag_step.label_filter and not any(
            e.get("name") == "RESULTS_DIR" for e in env
        ):
            env.append({"name": "RESULTS_DIR", "value": "/workspace"})

        resources = (
            _render_resources(dag_step.resources, full_ctx, jinja_env)
            if dag_step.resources
            else None
        )

        pod_ctx = {
            "pod_name": pod_name,
            "namespace": namespace,
            "managed_by_label": tc.managed_by_label,
            "test": test.name,
            "node": node,
            "sweep_id": sweep_id,
            "dag_step_name": dag_step.name,
            "node_selector_key": tc.node_selector_key,
            "image": dag_step.image,
            "command": pod_command,
            "env": env,
            "ports": dag_step.ports,
            "resources": resources,
            "volume_mounts": dag_step.volume_mounts,
            "volumes": dag_step.volumes,
            "pvc": pvc,
            "privileged": dag_step.privileged,
            "workspace_subpath": workspace_subpath,
            "binaries_subpath": binaries_subpath,
        }
        content = render_manifest(jinja_env, "test-pod.yaml.j2", pod_ctx)

        if dag_step.service.enabled:
            svc_ctx = {
                "service_name": svc_name,
                "pod_name": "",
                "port": dag_step.service.port,
                "namespace": namespace,
                "node": node,
                "test": test.name,
                "dag_step_name": dag_step.name,
                "managed_by_label": tc.managed_by_label,
                "headless": dag_step.service.headless,
                "sweep_id": sweep_id,
            }
            svc_content = render_manifest(jinja_env, "dag-service.yaml.j2", svc_ctx)
            content = content + "\n---\n" + svc_content

        gen_config: dict[str, Any] = {"output": "manifest"}
        if dag_step.service.enabled:
            gen_config["service_name"] = svc_name

        steps.append(
            Step(
                name=step_name,
                type="generate",
                config=gen_config,
                content=content,
                resource_name=res_name,
                node=node,
                test=test.name,
                test_id=test.test_id,
                on_failure=test.on_failure,
                scope="node",
                phase="test",
            )
        )
        steps.append(
            Step(
                name=step_name,
                type="command",
                config={
                    "command": "apply",
                    "probe": "poll-completed",
                    "pod_name": pod_name,
                    "timeout": test.timeout or tc.default_test_timeout,
                },
                source=[step_name],
                resource_name=res_name,
                node=node,
                test=test.name,
                test_id=test.test_id,
                on_failure=test.on_failure,
                scope="node",
                phase="test",
            )
        )
        selector = f"test={test.name},node={node},sweep={sweep_id}"
        steps.append(
            Step(
                name=cleanup_name,
                type="command",
                config={
                    "command": "delete",
                    "probe": "none",
                    "selector": selector,
                },
                resource_name=cleanup_res,
                node=node,
                test=test.name,
                test_id=test.test_id,
                on_failure=test.on_failure,
                scope="node",
                phase="test",
            )
        )


def _render_env(
    env: list[dict[str, Any]],
    ctx: dict[str, Any],
    jinja_env: Environment,
) -> list[dict[str, Any]]:
    result = []
    for e in env:
        rendered = dict(e)
        if "value" in rendered:
            rendered["value"] = render_string(jinja_env, str(rendered["value"]), ctx)
        result.append(rendered)
    return result


def _render_resources(
    resources: dict[str, Any],
    ctx: dict[str, Any],
    jinja_env: Environment,
) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for section, values in resources.items():
        if isinstance(values, dict):
            result[section] = {
                k: render_string(jinja_env, str(v), ctx) for k, v in values.items()
            }
        else:
            result[section] = values
    return result
