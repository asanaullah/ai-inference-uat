# Assisted by Claude Opus 4.6
"""Setup/teardown step computation, step validation, and step I/O."""

import json
import re
from dataclasses import asdict
from pathlib import Path

import yaml
from jinja2 import Environment
from pydantic import ValidationError

from .cluster import compute_cluster_steps
from .common import (
    load_config,
    load_tool_config,
    render_manifest,
    render_template,
    sanitize_node_name,
    validate_node_resources,
)
from .models import ClusterTestSpec, LoadedTest, Step, StepsFile, ToolConfig
from .node import compute_node_steps
from .project import compute_project_steps


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


def compute_setup_steps(
    tests: list[LoadedTest],
    tc: ToolConfig,
    cs: ClusterTestSpec,
    jinja_env: Environment,
    cluster_path: str,
    suite_path: str,
    aggregate_py: str,
) -> list[Step]:
    assert tests, "No tests loaded"
    assert aggregate_py, "aggregate.py content is empty"

    steps: list[Step] = []

    files: dict[str, str] = {}
    for t in tests:
        assert t.go_source, f"Test {t.name} has empty Go source"
        files[f"{t.name}_test.go"] = t.go_source

    files["cluster.yaml"] = Path(cluster_path).read_text()
    files["test_suite.yaml"] = Path(suite_path).read_text()
    files["build.sh"] = render_template(
        jinja_env,
        "build.sh.j2",
        {
            "tests": list(dict.fromkeys(t.name for t in tests)),
            "ginkgo_version": tc.ginkgo_version,
        },
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


def write_steps_file(
    steps: list[Step],
    tc: ToolConfig,
    cs: ClusterTestSpec,
    path: Path,
    set_mappings: dict[str, dict[str, list[str]]] | None = None,
) -> None:
    metadata: dict = {
        "toolConfig": tc.model_dump(by_alias=True),
        "clusterSpec": cs.model_dump(by_alias=True),
    }
    if set_mappings:
        metadata["setMappings"] = set_mappings
    data = {
        "metadata": metadata,
        "steps": [asdict(s) for s in steps],
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2))


def load_steps_file(
    path: Path,
) -> tuple[list[Step], ToolConfig, ClusterTestSpec]:
    with open(path) as f:
        data = json.load(f)

    sf = StepsFile(**data)

    tc = ToolConfig(**sf.metadata["toolConfig"])
    cs = ClusterTestSpec(**sf.metadata["clusterSpec"])

    steps = [Step(**s) for s in sf.steps]
    _validate_unique_pod_names(steps)
    _validate_service_names(steps)

    return steps, tc, cs


def generate_steps(
    *,
    config_path: str,
    test_suite_path: str,
    test_lib_path: str,
    cluster_path: str,
    scripts_dir: str,
    output_dir: Path,
    jinja_env: Environment,
) -> tuple[list[Step], ToolConfig, ClusterTestSpec]:
    try:
        tc = load_tool_config(config_path)
    except FileNotFoundError:
        print(f"Error: config file not found: {config_path}")
        raise SystemExit(1)
    except yaml.YAMLError as e:
        print(f"Error: invalid YAML in {config_path}: {e}")
        raise SystemExit(1)
    except ValidationError as e:
        print(f"Error: invalid config in {config_path}:\n{e}")
        raise SystemExit(1)

    try:
        suite, cluster, tests = load_config(
            test_suite_path, test_lib_path, cluster_path
        )
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
    for node_spec in cs.nodes:
        node_spec.sanitized_name = sanitize_node_name(node_spec.name)

    print(f"Cluster: {Path(cluster_path).stem}")
    print(f"Namespace: {cs.namespace}")
    print(f"Nodes: {[n.name for n in cs.nodes]}")
    print(f"Tests: {[(t.name, t.scope, t.on_failure) for t in suite.spec.tests]}")
    print(f"Tests loaded: {[t.name for t in tests]}")

    scripts = Path(scripts_dir)
    try:
        aggregate_py = (scripts / "aggregate.py").read_text()
    except FileNotFoundError:
        print(f"Error: aggregate.py not found in {scripts_dir}")
        raise SystemExit(1)

    try:
        setup_steps = compute_setup_steps(
            tests,
            tc,
            cs,
            jinja_env,
            cluster_path,
            test_suite_path,
            aggregate_py,
        )
    except Exception as e:
        print(f"Error computing setup steps: {e}")
        raise SystemExit(1)

    test_steps: list[Step] = []
    all_set_mappings: dict[str, dict[str, list[str]]] = {}
    for test in tests:
        print(f"Processing test: {test.name} (scope: {test.scope})")
        try:
            if test.scope == "node":
                for node_spec in cs.nodes:
                    validate_node_resources(test, node_spec, jinja_env)
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
                cluster_steps, set_mappings = compute_cluster_steps(
                    test,
                    tc,
                    cs.namespace,
                    cs.storage.pvc,
                    cs.storage.base_path,
                    jinja_env,
                    nodes=cs.nodes,
                )
                test_steps.extend(cluster_steps)
                if set_mappings:
                    all_set_mappings[test.test_id] = set_mappings
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

    try:
        write_steps_file(
            all_steps,
            tc,
            cs,
            output_dir / "steps.json",
            set_mappings=all_set_mappings or None,
        )
    except Exception as e:
        print(f"Error writing steps.json: {e}")
        raise SystemExit(1)
    print(f"Steps DAG written to {output_dir / 'steps.json'}")

    return all_steps, tc, cs
