# Assisted by Claude Opus 4.6
"""Node-level step computation, DAG/test pod rendering, and requirement checks."""

from __future__ import annotations

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
    tests: list[LoadedTest],
    tool_config: ToolConfig,
    namespace: str,
    pvc: str,
    base_path: str,
    jinja_env: Environment,
    stop_on_failure: bool,
) -> list[Step]:
    steps: list[Step] = []
    node = node_spec.name
    node_spec_dict = node_spec.model_dump(by_alias=True)

    for test in tests:
        if not node_meets_requirements(test.spec.requirements, node_spec):
            print(f"  Skipping {test.name} on {node} (requirements not met)")
            continue

        services: dict[str, dict[str, Any]] = {}
        has_persistent = False
        on_error = 'continue' if not stop_on_failure else 'stop'

        for dag_step in test.spec.dag:
            if dag_step.persists_through_sweep:
                has_persistent = True
                _add_persistent_steps(
                    steps, dag_step, node, test, tool_config, namespace,
                    pvc, base_path, node_spec_dict, services, jinja_env,
                )
            else:
                _add_ephemeral_steps(
                    steps, dag_step, node, test, tool_config, namespace,
                    pvc, base_path, node_spec_dict, services, jinja_env,
                    on_error,
                )

        if has_persistent:
            selector = f'test={test.name},node={node}'
            steps.append(Step(
                name=f'teardown-{test.name}',
                type='command',
                config={
                    'command': 'delete',
                    'probe': 'none',
                    'onError': 'stop',
                    'selector': selector,
                },
                node=node,
                test=test.name,
            ))
            steps.append(Step(
                name=f'finally-teardown-{test.name}',
                type='command',
                config={
                    'command': 'delete',
                    'probe': 'none',
                    'onError': 'run',
                    'selector': selector,
                },
                node=node,
                test=test.name,
            ))

    return steps


# Node-specific helpers: these use nodeSelector, node-prefixed pod names, and
# node-scoped labels. Kept here because cluster/project scopes are not yet
# implemented, so the right abstraction boundary is unknown.

def _add_persistent_steps(
    steps: list[Step],
    dag_step: DAGStep,
    node: str,
    test: LoadedTest,
    tc: ToolConfig,
    namespace: str,
    pvc: str,
    base_path: str,
    node_spec_dict: dict,
    services: dict,
    jinja_env: Environment,
) -> None:
    pod_name = f'{node}-{dag_step.name}'

    render_ctx = {
        'timestamp': '__TIMESTAMP__',
        'node': node,
        'serverConfig': test.spec.server_config,
        'nodeSpec': node_spec_dict,
    }

    command = None
    if dag_step.command:
        cmd_list = build_command(dag_step.command.args, dag_step.command.flags)
        cmd_list = [render_string(jinja_env, str(c), render_ctx) for c in cmd_list]
        command = cmd_list

    env = _render_env(dag_step.env, render_ctx, jinja_env)
    resources = (
        _render_resources(dag_step.resources, {'nodeSpec': node_spec_dict}, jinja_env)
        if dag_step.resources else None
    )

    if dag_step.service.enabled:
        svc_name = f'{node}-{dag_step.service.name}'
        services[dag_step.service.name] = {
            'url': f'http://{svc_name}:{dag_step.service.port}',
            'name': svc_name,
            'port': dag_step.service.port,
        }

    workspace_subpath = f'{base_path}/__TIMESTAMP__/node/{node}/{test.name}/{dag_step.name}'
    binaries_subpath = f'{base_path}/__TIMESTAMP__/binaries'

    pod_ctx = {
        'pod_name': pod_name,
        'namespace': namespace,
        'managed_by_label': tc.managed_by_label,
        'test': test.name,
        'node': node,
        'node_selector_key': tc.node_selector_key,
        'image': dag_step.image,
        'command': command,
        'env': env,
        'ports': dag_step.ports,
        'readiness_probe': dag_step.readiness_probe,
        'resources': resources,
        'volume_mounts': dag_step.volume_mounts,
        'volumes': dag_step.volumes,
        'pvc': pvc,
        'privileged': dag_step.privileged,
        'workspace_subpath': workspace_subpath,
        'binaries_subpath': binaries_subpath,
    }
    content = render_manifest(jinja_env, 'dag-pod.yaml.j2', pod_ctx)

    if dag_step.service.enabled:
        svc_ctx = {
            'service_name': services[dag_step.service.name]['name'],
            'pod_name': pod_name,
            'port': dag_step.service.port,
            'namespace': namespace,
            'node': node,
            'test': test.name,
            'managed_by_label': tc.managed_by_label,
        }
        svc_content = render_manifest(jinja_env, 'dag-service.yaml.j2', svc_ctx)
        content = content + '\n---\n' + svc_content

    gen_name = f'{node}-{dag_step.name}'
    steps.append(Step(
        name=gen_name,
        type='generate',
        config={'output': 'manifest', 'onError': 'stop'},
        content=content,
        node=node,
        test=test.name,
    ))
    steps.append(Step(
        name=f'deploy-{dag_step.name}',
        type='command',
        config={
            'command': 'apply',
            'probe': 'wait-ready',
            'onError': 'stop',
            'pod_name': pod_name,
            'timeout': tc.deploy_timeout,
        },
        source=[gen_name],
        node=node,
        test=test.name,
    ))


def _add_ephemeral_steps(
    steps: list[Step],
    dag_step: DAGStep,
    node: str,
    test: LoadedTest,
    tc: ToolConfig,
    namespace: str,
    pvc: str,
    base_path: str,
    node_spec_dict: dict,
    services: dict,
    jinja_env: Environment,
    on_error: str,
) -> None:
    if dag_step.parameter_sweep:
        entries = [
            (e.id, e.description, {**dag_step.parameter_sweep.base_command.flags, **e.flags})
            for e in dag_step.parameter_sweep.entries
        ]
    else:
        entries = [(dag_step.name, '', {})]

    for sweep_id, _sweep_desc, sweep_flags in entries:
        param_sweep: dict[str, Any] = {'id': sweep_id}
        workspace_subpath = f'{base_path}/__TIMESTAMP__/node/{node}/{test.name}/{sweep_id}'
        binaries_subpath = f'{base_path}/__TIMESTAMP__/binaries'

        render_ctx: dict[str, Any] = {
            'timestamp': '__TIMESTAMP__',
            'node': node,
            'serverConfig': test.spec.server_config,
            'services': services,
            'nodeSpec': node_spec_dict,
        }

        if dag_step.parameter_sweep:
            args = dag_step.parameter_sweep.base_command.args
            sweep_cmd = build_command(args, sweep_flags)
            sweep_cmd = [render_string(jinja_env, str(v), render_ctx) for v in sweep_cmd]
            param_sweep['command'] = sweep_cmd

        full_ctx = {**render_ctx, 'paramSweep': param_sweep}

        if dag_step.label_filter:
            binary = f'/binaries/{test.name}/test.bin'
            pod_command = [
                binary,
                f'--ginkgo.label-filter={dag_step.label_filter}',
                '--ginkgo.junit-report=/workspace/junit.xml',
            ]
        elif dag_step.command:
            if dag_step.parameter_sweep:
                pod_command = build_command(
                    dag_step.parameter_sweep.base_command.args, sweep_flags,
                )
            else:
                pod_command = build_command(dag_step.command.args, dag_step.command.flags)
            pod_command = [render_string(jinja_env, str(v), full_ctx) for v in pod_command]
        else:
            pod_command = None

        env = _render_env(list(dag_step.env), full_ctx, jinja_env)

        if dag_step.label_filter and not any(e.get('name') == 'RESULTS_DIR' for e in env):
            env.append({'name': 'RESULTS_DIR', 'value': '/workspace'})

        resources = (
            _render_resources(dag_step.resources, full_ctx, jinja_env)
            if dag_step.resources else None
        )

        pod_name = f'{node}-test-{test.name}-{sweep_id}'
        pod_ctx = {
            'pod_name': pod_name,
            'namespace': namespace,
            'managed_by_label': tc.managed_by_label,
            'test': test.name,
            'node': node,
            'step_id': sweep_id,
            'node_selector_key': tc.node_selector_key,
            'image': dag_step.image,
            'command': pod_command,
            'env': env,
            'resources': resources,
            'volume_mounts': dag_step.volume_mounts,
            'volumes': dag_step.volumes,
            'pvc': pvc,
            'privileged': dag_step.privileged,
            'workspace_subpath': workspace_subpath,
            'binaries_subpath': binaries_subpath,
        }
        content = render_manifest(jinja_env, 'test-pod.yaml.j2', pod_ctx)

        gen_name = f'{node}-{test.name}-{sweep_id}'
        steps.append(Step(
            name=gen_name,
            type='generate',
            config={'output': 'manifest', 'onError': on_error},
            content=content,
            node=node,
            test=test.name,
        ))
        steps.append(Step(
            name=f'run-test-{test.name}-{sweep_id}',
            type='command',
            config={
                'command': 'apply',
                'probe': 'poll-completed',
                'onError': on_error,
                'pod_name': pod_name,
                'timeout': tc.test_timeout,
            },
            source=[gen_name],
            node=node,
            test=test.name,
        ))
        selector = f'test={test.name},node={node},step={sweep_id}'
        steps.append(Step(
            name=f'cleanup-{test.name}-{sweep_id}',
            type='command',
            config={
                'command': 'delete',
                'probe': 'none',
                'onError': 'continue',
                'selector': selector,
            },
            node=node,
            test=test.name,
        ))


def _render_env(
    env: list[dict[str, Any]], ctx: dict[str, Any], jinja_env: Environment,
) -> list[dict[str, Any]]:
    result = []
    for e in env:
        rendered = dict(e)
        if 'value' in rendered:
            rendered['value'] = render_string(jinja_env, str(rendered['value']), ctx)
        result.append(rendered)
    return result


def _render_resources(
    resources: dict[str, Any], ctx: dict[str, Any], jinja_env: Environment,
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
