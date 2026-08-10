"""Integration tests for peer namespace override on DAG steps."""

import pytest
import yaml

from src.cluster import compute_cluster_steps
from src.common import create_jinja_env
from src.models import (
    ClusterTestSpec,
    LoadedTest,
    NodeSpec,
    TestSpec,
    ToolConfig,
)
from src.node import compute_node_steps
from src.project import compute_project_steps
from src.writers.manual import _derive_manual_script
from src.writers.tekton import _render_tekton_task

TC_DATA = {
    "oseCLIImage": "ose:latest",
    "builderImage": "golang:1.25",
    "aggregatorImage": "python:3-slim",
    "configmapName": "cm",
    "builderPodName": "builder",
    "aggregatorPodName": "agg",
    "nodeSelectorKey": "kubernetes.io/hostname",
    "managedByLabel": "uat",
}

NAMESPACE = "uat-project"
PEER_NAMESPACE = "uat-peer"


@pytest.fixture()
def env():
    return create_jinja_env("templates")


@pytest.fixture()
def tc():
    return ToolConfig(**TC_DATA)


@pytest.fixture()
def cs():
    return ClusterTestSpec(
        nodes=[
            {"name": "wrk-1", "componentValidation": {"sanity": {"nvidia.com/gpu": 4}}}
        ],
        namespace=NAMESPACE,
        peerNamespace=PEER_NAMESPACE,
        storage={"pvc": "pvc", "basePath": "results"},
    )


def _node(name="wrk-1", gpu_count=4):
    return NodeSpec(
        name=name,
        componentValidation={"sanity": {"nvidia.com/gpu": gpu_count}},
    )


def _test(dag, scope="node", name="t", test_id="t1"):
    spec = TestSpec(source={"ginkgo": "t.go"}, dag=dag)
    return LoadedTest(
        name=name,
        spec=spec,
        go_source="x",
        test_id=test_id,
        scope=scope,
    )


def _peer_dag():
    return [
        {
            "name": "server",
            "image": "vllm:latest",
            "persistsThroughSweep": True,
        },
        {
            "name": "client",
            "image": "perf:latest",
            "peer": True,
            "labelFilter": "pass-fail",
        },
    ]


# -- Step.namespace field ------------------------------------------------------


class TestNodePeerNamespace:
    def test_peer_step_gets_peer_namespace(self, env, tc):
        steps = compute_node_steps(
            _node(),
            _test(_peer_dag()),
            tc,
            NAMESPACE,
            "pvc",
            "results",
            env,
            peer_namespace=PEER_NAMESPACE,
        )
        server_steps = [s for s in steps if "server" in s.name and s.phase == "test"]
        client_steps = [s for s in steps if "client" in s.name and s.phase == "test"]
        for s in server_steps:
            assert s.namespace == NAMESPACE
        for s in client_steps:
            assert s.namespace == PEER_NAMESPACE

    def test_non_peer_step_gets_main_namespace(self, env, tc):
        dag = [{"name": "run", "image": "img", "labelFilter": "pass-fail"}]
        steps = compute_node_steps(
            _node(),
            _test(dag),
            tc,
            NAMESPACE,
            "pvc",
            "results",
            env,
            peer_namespace=PEER_NAMESPACE,
        )
        test_steps = [s for s in steps if s.phase == "test" and not s.lifecycle]
        for s in test_steps:
            assert s.namespace == NAMESPACE

    def test_peer_manifest_targets_peer_namespace(self, env, tc):
        steps = compute_node_steps(
            _node(),
            _test(_peer_dag()),
            tc,
            NAMESPACE,
            "pvc",
            "results",
            env,
            peer_namespace=PEER_NAMESPACE,
        )
        client_gen = next(
            s for s in steps if "client" in s.name and s.type == "generate"
        )
        doc = yaml.safe_load(client_gen.content)
        assert doc["metadata"]["namespace"] == PEER_NAMESPACE

    def test_server_manifest_targets_main_namespace(self, env, tc):
        steps = compute_node_steps(
            _node(),
            _test(_peer_dag()),
            tc,
            NAMESPACE,
            "pvc",
            "results",
            env,
            peer_namespace=PEER_NAMESPACE,
        )
        server_gen = next(
            s for s in steps if "server" in s.name and s.type == "generate"
        )
        doc = yaml.safe_load(server_gen.content)
        assert doc["metadata"]["namespace"] == NAMESPACE


class TestProjectPeerNamespace:
    def test_peer_step_gets_peer_namespace(self, env, tc):
        steps = compute_project_steps(
            _test(_peer_dag(), scope="project"),
            tc,
            NAMESPACE,
            "pvc",
            "results",
            env,
            peer_namespace=PEER_NAMESPACE,
        )
        client_steps = [s for s in steps if "client" in s.name and s.phase == "test"]
        for s in client_steps:
            assert s.namespace == PEER_NAMESPACE

    def test_non_peer_step_gets_main_namespace(self, env, tc):
        steps = compute_project_steps(
            _test(_peer_dag(), scope="project"),
            tc,
            NAMESPACE,
            "pvc",
            "results",
            env,
            peer_namespace=PEER_NAMESPACE,
        )
        server_steps = [s for s in steps if "server" in s.name and s.phase == "test"]
        for s in server_steps:
            assert s.namespace == NAMESPACE

    def test_peer_step_uses_peer_pvc(self, env, tc):
        steps = compute_project_steps(
            _test(_peer_dag(), scope="project"),
            tc,
            NAMESPACE,
            "pvc",
            "results",
            env,
            peer_namespace=PEER_NAMESPACE,
            peer_pvc="peer-pvc",
            peer_base_path="peer-results",
        )
        client_gen = next(
            s for s in steps if "client" in s.name and s.type == "generate"
        )
        doc = yaml.safe_load(client_gen.content)
        volumes = {v["name"]: v for v in doc["spec"]["volumes"]}
        assert volumes["workspace"]["persistentVolumeClaim"]["claimName"] == "peer-pvc"

    def test_non_peer_step_uses_main_pvc(self, env, tc):
        steps = compute_project_steps(
            _test(_peer_dag(), scope="project"),
            tc,
            NAMESPACE,
            "pvc",
            "results",
            env,
            peer_namespace=PEER_NAMESPACE,
            peer_pvc="peer-pvc",
            peer_base_path="peer-results",
        )
        server_gen = next(
            s for s in steps if "server" in s.name and s.type == "generate"
        )
        doc = yaml.safe_load(server_gen.content)
        volumes = {v["name"]: v for v in doc["spec"]["volumes"]}
        assert volumes["workspace"]["persistentVolumeClaim"]["claimName"] == "pvc"

    def test_peer_step_uses_peer_base_path(self, env, tc):
        steps = compute_project_steps(
            _test(_peer_dag(), scope="project"),
            tc,
            NAMESPACE,
            "pvc",
            "results",
            env,
            peer_namespace=PEER_NAMESPACE,
            peer_pvc="peer-pvc",
            peer_base_path="peer-results",
        )
        client_gen = next(
            s for s in steps if "client" in s.name and s.type == "generate"
        )
        doc = yaml.safe_load(client_gen.content)
        mounts = doc["spec"]["containers"][0]["volumeMounts"]
        workspace_mount = next(m for m in mounts if m["mountPath"] == "/uat_workspace")
        assert workspace_mount["subPath"].startswith("peer-results/")

    def test_peer_step_falls_back_to_main_pvc(self, env, tc):
        steps = compute_project_steps(
            _test(_peer_dag(), scope="project"),
            tc,
            NAMESPACE,
            "pvc",
            "results",
            env,
            peer_namespace=PEER_NAMESPACE,
        )
        client_gen = next(
            s for s in steps if "client" in s.name and s.type == "generate"
        )
        doc = yaml.safe_load(client_gen.content)
        volumes = {v["name"]: v for v in doc["spec"]["volumes"]}
        assert volumes["workspace"]["persistentVolumeClaim"]["claimName"] == "pvc"


class TestClusterPeerNamespace:
    def test_peer_step_gets_peer_namespace(self, env, tc):
        nodes = [_node("wrk-1")]
        steps, _ = compute_cluster_steps(
            _test(_peer_dag(), scope="cluster"),
            tc,
            NAMESPACE,
            "pvc",
            "results",
            env,
            nodes=nodes,
            peer_namespace=PEER_NAMESPACE,
        )
        client_steps = [s for s in steps if "client" in s.name and s.phase == "test"]
        for s in client_steps:
            assert s.namespace == PEER_NAMESPACE

    def test_non_peer_step_gets_main_namespace(self, env, tc):
        nodes = [_node("wrk-1")]
        steps, _ = compute_cluster_steps(
            _test(_peer_dag(), scope="cluster"),
            tc,
            NAMESPACE,
            "pvc",
            "results",
            env,
            nodes=nodes,
            peer_namespace=PEER_NAMESPACE,
        )
        server_steps = [s for s in steps if "server" in s.name and s.phase == "test"]
        for s in server_steps:
            assert s.namespace == NAMESPACE


# -- Manual writer uses per-step namespace ------------------------------------


class TestManualPeerNamespace:
    def test_apply_uses_peer_namespace(self, env):
        from src.models import Step

        step = Step(
            name="ap",
            type="command",
            config={"command": "apply"},
            source=["my-manifest"],
            namespace=PEER_NAMESPACE,
        )
        effective_ns = step.namespace or NAMESPACE
        script = _derive_manual_script(step, env, effective_ns)
        assert f"-n {PEER_NAMESPACE}" in script
        assert f"-n {NAMESPACE}" not in script

    def test_delete_uses_peer_namespace(self, env):
        from src.models import Step

        step = Step(
            name="td",
            type="command",
            config={"command": "delete", "selector": "app=x"},
            namespace=PEER_NAMESPACE,
        )
        effective_ns = step.namespace or NAMESPACE
        script = _derive_manual_script(step, env, effective_ns)
        assert f"-n {PEER_NAMESPACE}" in script

    def test_exec_uses_peer_namespace(self, env):
        from src.models import Step

        step = Step(
            name="ex",
            type="command",
            config={"command": "exec", "target": "pod", "args": ["bash"]},
            namespace=PEER_NAMESPACE,
        )
        effective_ns = step.namespace or NAMESPACE
        script = _derive_manual_script(step, env, effective_ns)
        assert f"-n {PEER_NAMESPACE}" in script

    def test_no_peer_uses_main_namespace(self, env):
        from src.models import Step

        step = Step(
            name="ap",
            type="command",
            config={"command": "apply"},
            source=["my-manifest"],
            namespace="",
        )
        effective_ns = step.namespace or NAMESPACE
        script = _derive_manual_script(step, env, effective_ns)
        assert f"-n {NAMESPACE}" in script


# -- Tekton writer uses per-step namespace ------------------------------------


class TestTektonPeerNamespace:
    def _manifest(self):
        return (
            "apiVersion: v1\nkind: Pod\nmetadata:\n  name: test-pod\n  namespace: ns\n"
        )

    def test_apply_task_uses_peer_namespace(self, env, tc, cs):
        from src.models import Step

        step = Step(
            name="s",
            type="command",
            config={"command": "apply", "probe": "wait-ready", "pod_name": "p"},
            namespace=PEER_NAMESPACE,
        )
        out = _render_tekton_task(step, self._manifest(), "s", [], tc, cs, env)
        doc = yaml.safe_load(out)
        assert doc["metadata"]["namespace"] == PEER_NAMESPACE

    def test_delete_task_uses_peer_namespace(self, env, tc, cs):
        from src.models import Step

        step = Step(
            name="s",
            type="command",
            config={"command": "delete", "selector": "app=x"},
            namespace=PEER_NAMESPACE,
        )
        out = _render_tekton_task(step, "", "s", [], tc, cs, env)
        assert PEER_NAMESPACE in out

    def test_no_peer_uses_main_namespace(self, env, tc, cs):
        from src.models import Step

        step = Step(
            name="s",
            type="command",
            config={"command": "apply", "probe": "wait-ready", "pod_name": "p"},
            namespace="",
        )
        out = _render_tekton_task(step, self._manifest(), "s", [], tc, cs, env)
        doc = yaml.safe_load(out)
        assert doc["metadata"]["namespace"] == NAMESPACE


# -- End-to-end: compute steps then verify manual scripts ---------------------


class TestEndToEndPeerManual:
    def test_node_peer_step_manual_script_uses_peer_ns(self, env, tc):
        steps = compute_node_steps(
            _node(),
            _test(_peer_dag()),
            tc,
            NAMESPACE,
            "pvc",
            "results",
            env,
            peer_namespace=PEER_NAMESPACE,
        )
        client_cmd = next(
            s
            for s in steps
            if "client" in s.name
            and s.type == "command"
            and s.config.get("command") == "apply"
        )
        effective_ns = client_cmd.namespace or NAMESPACE
        script = _derive_manual_script(client_cmd, env, effective_ns)
        assert f"-n {PEER_NAMESPACE}" in script

    def test_node_non_peer_step_manual_script_uses_main_ns(self, env, tc):
        steps = compute_node_steps(
            _node(),
            _test(_peer_dag()),
            tc,
            NAMESPACE,
            "pvc",
            "results",
            env,
            peer_namespace=PEER_NAMESPACE,
        )
        server_cmd = next(
            s
            for s in steps
            if "server" in s.name
            and s.type == "command"
            and s.config.get("command") == "apply"
        )
        effective_ns = server_cmd.namespace or NAMESPACE
        script = _derive_manual_script(server_cmd, env, effective_ns)
        assert f"-n {NAMESPACE}" in script
        assert f"-n {PEER_NAMESPACE}" not in script
