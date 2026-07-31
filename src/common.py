# Assisted by Claude Opus 4.6
"""Jinja2 engine, manifest validation, config loading, and shared utilities."""

import hashlib
import json
import re
import shlex
from pathlib import Path
from typing import Any

import yaml
from jinja2 import Environment, FileSystemLoader, StrictUndefined

from .models import (
    ClusterTest,
    DAGStep,
    LoadedTest,
    ModelsStorageConfig,
    NodeSpec,
    Step,
    Test,
    TestSpec,
    TestSuite,
    ToolConfig,
)


def create_jinja_env(template_dir: str | Path) -> Environment:
    env = Environment(
        loader=FileSystemLoader(str(template_dir)),
        undefined=StrictUndefined,
        trim_blocks=True,
        lstrip_blocks=True,
        keep_trailing_newline=True,
    )
    env.filters["to_yaml"] = _to_yaml
    env.filters["toJson"] = _to_json
    env.filters["yaml_quote"] = _yaml_quote
    env.filters["shell_join"] = _shell_join
    return env


def _to_yaml(value: Any) -> str:
    return yaml.dump(value, default_flow_style=False).rstrip("\n")


def _to_json(value: Any) -> str:
    return json.dumps(value)


_YAML11_BOOLEANS = frozenset(
    {
        "true",
        "false",
        "yes",
        "no",
        "on",
        "off",
        "True",
        "False",
        "Yes",
        "No",
        "On",
        "Off",
        "TRUE",
        "FALSE",
        "YES",
        "NO",
        "ON",
        "OFF",
    }
)


def _yaml_quote(value: str) -> str:
    s = str(value)
    if not s or any(c in s for c in ":{}[],\"'|>&*#?!%@") or s != s.strip():
        return json.dumps(s)
    if s in _YAML11_BOOLEANS or s in ("null", "Null", "NULL", "~"):
        return json.dumps(s)
    try:
        float(s)
        return json.dumps(s)
    except ValueError:
        pass
    return s


def _shell_join(value: list[str]) -> str:
    return shlex.join(value)


def render_template(
    env: Environment, template_name: str, context: dict[str, Any]
) -> str:
    template = env.get_template(template_name)
    return template.render(context)


def render_manifest(
    env: Environment, template_name: str, context: dict[str, Any]
) -> str:
    content = render_template(env, template_name, context)
    validate_manifest(content)
    return content


def render_string(
    env: Environment, template_string: str, context: dict[str, Any]
) -> str:
    template = env.from_string(template_string)
    return template.render(context)


# Checks structural minimums only; additional validation (e.g. schema
# validation, dry-run) can be added in the future.
def validate_manifest(content: str) -> None:
    for doc in yaml.safe_load_all(content):
        if doc is None:
            continue
        if not isinstance(doc, dict):
            raise TypeError(f"Manifest document is not a mapping: {doc}")
        for required in ("apiVersion", "kind"):
            if required not in doc:
                raise ValueError(
                    f"Manifest missing required field '{required}': "
                    f"{doc.get('metadata', {}).get('name', '<unknown>')}"
                )
        metadata = doc.get("metadata", {})
        if "name" not in metadata and "generateName" not in metadata:
            raise ValueError(
                f"Manifest missing metadata.name or metadata.generateName: "
                f"{doc.get('kind', '<unknown>')}"
            )


def _deep_merge_spec(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    result = dict(base)
    for key, value in override.items():
        if key == "dag":
            if not isinstance(value, dict):
                raise TypeError(
                    "dag override must be a dict keyed by step name, not a list"
                )
            else:
                steps = [dict(s) for s in result.get("dag", [])]
                steps_by_name = {s["name"]: s for s in steps}
                for step_name, step_overrides in value.items():
                    if step_name not in steps_by_name:
                        raise ValueError(
                            f"dag override references unknown step '{step_name}'"
                        )
                    steps_by_name[step_name] = _deep_merge_spec(
                        steps_by_name[step_name], step_overrides
                    )
                result["dag"] = list(steps_by_name.values())
        elif (
            key in result and isinstance(result[key], dict) and isinstance(value, dict)
        ):
            result[key] = _deep_merge_spec(result[key], value)
        else:
            result[key] = value
    return result


_K8S_BINARY_SUFFIXES = {"Ki": 2**10, "Mi": 2**20, "Gi": 2**30, "Ti": 2**40}
_K8S_DECIMAL_SUFFIXES = {
    "n": 1e-9,
    "u": 1e-6,
    "k": 1e3,
    "M": 1e6,
    "G": 1e9,
    "T": 1e12,
}


def parse_k8s_quantity(value: Any) -> float:
    s = str(value).strip()
    if not s:
        return 0.0
    for suffix, multiplier in _K8S_BINARY_SUFFIXES.items():
        if s.endswith(suffix):
            return float(s[: -len(suffix)]) * multiplier
    if s.endswith("m"):
        return float(s[:-1]) / 1000.0
    for suffix, multiplier in _K8S_DECIMAL_SUFFIXES.items():
        if s.endswith(suffix):
            return float(s[:-1]) * multiplier
    return float(s)


def validate_node_resources(
    test: LoadedTest,
    node_spec: NodeSpec,
    jinja_env: Environment,
) -> None:
    node_spec_dict = node_spec.model_dump(by_alias=True)
    sanity_dict = node_spec.component_validation.sanity.model_dump(by_alias=True)
    render_ctx: dict[str, Any] = {
        "nodeSpec": node_spec_dict,
        "serverConfig": test.spec.server_config,
    }

    persistent_demand: dict[str, float] = {}
    ephemeral_demands: list[dict[str, float]] = []

    for dag_step in test.spec.dag:
        if not dag_step.resources:
            continue
        requests = dag_step.resources.get("requests", {})
        if not requests:
            continue

        rendered: dict[str, float] = {}
        for rkey, rval in requests.items():
            resolved = render_string(jinja_env, str(rval), render_ctx)
            rendered[rkey] = parse_k8s_quantity(resolved)

        if dag_step.persists_through_sweep:
            for rkey, rval in rendered.items():
                persistent_demand[rkey] = persistent_demand.get(rkey, 0.0) + rval
        else:
            ephemeral_demands.append(rendered)

    all_keys = set(persistent_demand.keys())
    for d in ephemeral_demands:
        all_keys.update(d.keys())

    for rkey in all_keys:
        max_eph = max((d.get(rkey, 0.0) for d in ephemeral_demands), default=0.0)
        peak = persistent_demand.get(rkey, 0.0) + max_eph
        capacity_raw = sanity_dict.get(rkey)
        if capacity_raw is None:
            continue
        capacity = parse_k8s_quantity(capacity_raw)
        if peak > capacity:
            raise ValueError(
                f"Test '{test.name}' on node '{node_spec.name}': "
                f"{rkey} demand ({peak}) exceeds capacity ({capacity})"
            )


def load_tool_config(config_path: str | Path) -> ToolConfig:
    with open(config_path) as f:
        data = yaml.safe_load(f)
    return ToolConfig(**data)


def load_config(
    suite_path: str | Path,
    lib_dir: str | Path,
    cluster_path: str | Path,
) -> tuple[TestSuite, ClusterTest, list[LoadedTest]]:
    lib_dir = Path(lib_dir)

    with open(suite_path) as f:
        suite = TestSuite(**yaml.safe_load(f))

    with open(cluster_path) as f:
        cluster = ClusterTest(**yaml.safe_load(f))

    tests: list[LoadedTest] = []
    for i, entry in enumerate(suite.spec.tests, 1):
        test_id = str(i)
        with open(lib_dir / f"{entry.name}.yaml") as f:
            test_def = Test(**yaml.safe_load(f))

        spec = test_def.spec
        if entry.spec:
            merged = _deep_merge_spec(spec.model_dump(by_alias=True), entry.spec)
            spec = TestSpec(**merged)

        go_source = (lib_dir / spec.source.ginkgo).read_text()

        tests.append(
            LoadedTest(
                name=entry.name,
                spec=spec,
                go_source=go_source,
                on_failure=entry.on_failure,
                timeout=entry.timeout,
                test_id=test_id,
                scope=entry.scope,
                placement=entry.placement,
            )
        )

    return suite, cluster, tests


def build_command(args: list[str], flags: dict[str, Any]) -> list[str]:
    cmd = list(args)
    for key, value in flags.items():
        cmd.append(f"--{key}={value}")
    return cmd


_INVALID_RFC1123 = re.compile(r"[^a-z0-9\-]")


def sanitize_node_name(name: str) -> str:
    sanitized = _INVALID_RFC1123.sub("-", name.lower()).strip("-")
    if len(sanitized) <= 16:
        return sanitized
    h = hashlib.sha256(name.encode()).hexdigest()[:4]
    return f"{sanitized[:12]}-{h}"


def add_teardown_steps(
    steps: list[Step],
    has_persistent: bool,
    step_prefix: str,
    res_prefix: str,
    selector: str,
    step_node: str,
    test: LoadedTest,
    scope: str,
) -> None:
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
                node=step_node,
                test=test.name,
                test_id=test.test_id,
                on_failure=test.on_failure,
                lifecycle=True,
                scope=scope,
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
            node=step_node,
            test=test.name,
            test_id=test.test_id,
            on_failure=test.on_failure,
            finally_step=True,
            lifecycle=True,
            scope=scope,
            phase="test",
        )
    )


def _build_render_ctx(
    node: str,
    test: LoadedTest,
    services: dict,
    node_spec_dict: dict | None = None,
) -> dict[str, Any]:
    ctx: dict[str, Any] = {
        "timestamp": "__TIMESTAMP__",
        "node": node,
        "serverConfig": test.spec.server_config,
        "services": services,
    }
    if node_spec_dict is not None:
        ctx["nodeSpec"] = node_spec_dict
    return ctx


def _register_service(
    dag_step: DAGStep,
    res_prefix: str,
    services: dict,
) -> str:
    if not dag_step.service.enabled:
        return ""
    svc_name = f"svc-{res_prefix}-{dag_step.service.name}"
    services[dag_step.service.name] = {
        "url": f"http://{svc_name}:{dag_step.service.port}",
        "name": svc_name,
        "port": dag_step.service.port,
    }
    return svc_name


def render_env(
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


def render_resources(
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


def add_persistent_steps(
    steps: list[Step],
    dag_step: DAGStep,
    step_prefix: str,
    res_prefix: str,
    node: str,
    step_node: str,
    test: LoadedTest,
    tc: ToolConfig,
    namespace: str,
    pvc: str,
    base_path: str,
    services: dict,
    jinja_env: Environment,
    scope: str,
    node_spec_dict: dict | None = None,
    chain: str = "",
    models_storage: "ModelsStorageConfig | None" = None,
) -> None:
    step_name = f"{step_prefix}-{dag_step.name}"
    pod_name = f"{res_prefix}-{dag_step.name}"

    render_ctx = _build_render_ctx(node, test, services, node_spec_dict)

    command = None
    if dag_step.command:
        cmd_list = build_command(dag_step.command.args, dag_step.command.flags)
        cmd_list = [render_string(jinja_env, str(c), render_ctx) for c in cmd_list]
        command = cmd_list

    env = render_env(dag_step.env, render_ctx, jinja_env)
    resources = (
        render_resources(dag_step.resources, render_ctx, jinja_env)
        if dag_step.resources
        else None
    )

    svc_name = _register_service(dag_step, res_prefix, services)

    workspace_subpath = f"{base_path}/__TIMESTAMP__/{step_name}"
    binaries_subpath = f"{base_path}/__TIMESTAMP__/binaries"

    pod_ctx: dict[str, Any] = {
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
        "models_storage": models_storage,
    }
    if chain:
        pod_ctx["chain"] = chain
    content = render_manifest(jinja_env, "dag-pod.yaml.j2", pod_ctx)

    if dag_step.service.enabled:
        svc_ctx: dict[str, Any] = {
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
        if chain:
            svc_ctx["chain"] = chain
        svc_content = render_manifest(jinja_env, "dag-service.yaml.j2", svc_ctx)
        content = content + "\n---\n" + svc_content

    gen_config: dict[str, Any] = {"output": "manifest"}
    if svc_name:
        gen_config["service_name"] = svc_name

    steps.append(
        Step(
            name=step_name,
            type="generate",
            config=gen_config,
            content=content,
            resource_name=pod_name,
            node=step_node,
            test=test.name,
            test_id=test.test_id,
            on_failure=test.on_failure,
            scope=scope,
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
            node=step_node,
            test=test.name,
            test_id=test.test_id,
            on_failure=test.on_failure,
            scope=scope,
            phase="test",
        )
    )


def add_ephemeral_steps(
    steps: list[Step],
    dag_step: DAGStep,
    step_prefix: str,
    res_prefix: str,
    node: str,
    step_node: str,
    test: LoadedTest,
    tc: ToolConfig,
    namespace: str,
    pvc: str,
    base_path: str,
    services: dict,
    jinja_env: Environment,
    scope: str,
    selector_extra: str = "",
    node_spec_dict: dict | None = None,
    chain: str = "",
    models_storage: "ModelsStorageConfig | None" = None,
) -> None:
    has_sweep = dag_step.parameter_sweep is not None

    if not has_sweep:
        svc_name = _register_service(dag_step, res_prefix, services)

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
            sweep_res_prefix = f"{res_prefix}-{sweep_id}"
            svc_name = _register_service(dag_step, sweep_res_prefix, services)
        else:
            step_name = f"{step_prefix}-{dag_step.name}"
            res_name = f"{res_prefix}-{dag_step.name}"
            cleanup_name = f"{step_prefix}-cleanup-{dag_step.name}"
            cleanup_res = f"{res_prefix}-cleanup-{dag_step.name}"

        pod_name = res_name
        param_sweep: dict[str, Any] = {"id": sweep_id}
        workspace_subpath = f"{base_path}/__TIMESTAMP__/{step_name}"
        binaries_subpath = f"{base_path}/__TIMESTAMP__/binaries"

        render_ctx = _build_render_ctx(node, test, services, node_spec_dict)

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
                "--ginkgo.junit-report=/uat_workspace/junit.xml",
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

        env = render_env(list(dag_step.env), full_ctx, jinja_env)

        if dag_step.label_filter and not any(
            e.get("name") == "RESULTS_DIR" for e in env
        ):
            env.append({"name": "RESULTS_DIR", "value": "/uat_workspace"})

        resources = (
            render_resources(dag_step.resources, full_ctx, jinja_env)
            if dag_step.resources
            else None
        )

        pod_ctx: dict[str, Any] = {
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
            "models_storage": models_storage,
        }
        if chain:
            pod_ctx["chain"] = chain
        content = render_manifest(jinja_env, "test-pod.yaml.j2", pod_ctx)

        if dag_step.service.enabled:
            svc_ctx: dict[str, Any] = {
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
            if chain:
                svc_ctx["chain"] = chain
            svc_content = render_manifest(jinja_env, "dag-service.yaml.j2", svc_ctx)
            content = content + "\n---\n" + svc_content

        gen_config: dict[str, Any] = {"output": "manifest"}
        if svc_name:
            gen_config["service_name"] = svc_name

        steps.append(
            Step(
                name=step_name,
                type="generate",
                config=gen_config,
                content=content,
                resource_name=res_name,
                node=step_node,
                test=test.name,
                test_id=test.test_id,
                on_failure=test.on_failure,
                scope=scope,
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
                node=step_node,
                test=test.name,
                test_id=test.test_id,
                on_failure=test.on_failure,
                scope=scope,
                phase="test",
            )
        )
        selector = f"test={test.name}{selector_extra},sweep={sweep_id}"
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
                node=step_node,
                test=test.name,
                test_id=test.test_id,
                on_failure=test.on_failure,
                lifecycle=True,
                scope=scope,
                phase="test",
            )
        )
