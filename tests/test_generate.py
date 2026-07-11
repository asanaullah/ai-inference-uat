import pytest

from src.common import create_jinja_env
from src.generate import (
    _build_generate_lookup,
    _derive_manual_script,
    _find_step,
    _render_tekton_task,
    _resolve_manifest,
    compute_setup_steps,
    compute_teardown_steps,
)
from src.models import ClusterTestSpec, LoadedTest, Step, TestSpec, ToolConfig


# -- Fixtures -----------------------------------------------------------------

TC_DATA = {
    "oseCLIImage": "ose:latest",
    "builderImage": "golang:1.25",
    "aggregatorImage": "python:3-slim",
    "configmapName": "uat-cm",
    "builderPodName": "builder",
    "aggregatorPodName": "agg",
    "nodeSelectorKey": "kubernetes.io/hostname",
    "managedByLabel": "uat",
}

CS_DATA = {
    "nodes": [{"name": "n", "componentValidation": {"sanity": {"gpuCount": 1}}}],
    "namespace": "ns",
    "storage": {"pvc": "pvc", "basePath": "results"},
}


@pytest.fixture()
def env():
    return create_jinja_env("templates")


@pytest.fixture()
def tc():
    return ToolConfig(**TC_DATA)


@pytest.fixture()
def cs():
    return ClusterTestSpec(**CS_DATA)


def _test(name="t"):
    spec = TestSpec(
        source={"ginkgo": "t.go", "goMod": "go.mod", "goSum": "go.sum"},
        dag=[{"name": "run", "image": "img", "labelFilter": "pass-fail"}],
    )
    return LoadedTest(name=name, spec=spec, go_source="src", go_mod="mod", go_sum="sum")


# -- Helpers ------------------------------------------------------------------


class TestHelpers:
    def test_build_generate_lookup(self):
        steps = [
            Step(name="a", type="generate", config={}, content="x"),
            Step(name="b", type="command", config={}),
        ]
        assert _build_generate_lookup(steps) == {"a": "x"}

    def test_resolve_manifest(self):
        lookup = {"gen": "manifest-content"}
        step = Step(name="cmd", type="command", config={}, source=["gen"])
        assert _resolve_manifest(step, lookup) == "manifest-content"

    def test_resolve_manifest_no_source(self):
        assert _resolve_manifest(Step(name="x", type="command", config={}), {}) == ""

    def test_find_step(self):
        steps = [
            Step(name="a", type="generate", config={}),
            Step(name="a", type="command", config={}),
        ]
        assert _find_step(steps, "a", "command").type == "command"
        assert _find_step(steps, "z", "command") is None


# -- _derive_manual_script ----------------------------------------------------


class TestDeriveManualScript:
    def test_exec(self, env):
        step = Step(
            name="build",
            type="command",
            config={
                "command": "exec",
                "target": "pod",
                "args": ["bash", "/run.sh"],
            },
        )
        script = _derive_manual_script(step, env)
        assert "oc exec pod -- bash /run.sh" in script

    def test_delete(self, env):
        step = Step(
            name="td",
            type="command",
            config={
                "command": "delete",
                "selector": "app=x",
            },
        )
        script = _derive_manual_script(step, env)
        assert "oc delete pods,services,deployments -l app=x --ignore-not-found" in script

    def test_delete_all(self, env):
        step = Step(
            name="cl",
            type="command",
            config={
                "command": "delete-all",
                "configmap_name": "cm",
                "managed_by_label": "uat",
            },
        )
        script = _derive_manual_script(step, env)
        assert "oc delete pods -l app.kubernetes.io/managed-by=uat --ignore-not-found" in script
        assert "oc delete services -l app.kubernetes.io/managed-by=uat --ignore-not-found" in script
        assert "oc delete configmap cm --ignore-not-found" in script

    def test_apply_returns_none(self, env):
        step = Step(name="ap", type="command", config={"command": "apply"})
        assert _derive_manual_script(step, env) is None


# -- _render_tekton_task ------------------------------------------------------


class TestRenderTektonTask:
    def _manifest(self):
        return (
            "apiVersion: v1\nkind: Pod\nmetadata:\n  name: test-pod\n  namespace: ns\n"
        )

    def test_apply_wait_ready(self, env, tc, cs):
        step = Step(
            name="s",
            type="command",
            config={
                "command": "apply",
                "probe": "wait-ready",
                "pod_name": "p",
            },
        )
        out = _render_tekton_task(step, self._manifest(), "s", [], tc, cs, env)
        assert "apiVersion: tekton.dev" in out
        assert "wait" in out.lower()

    def test_exec(self, env, tc, cs):
        step = Step(
            name="s",
            type="command",
            config={
                "command": "exec",
                "target": "pod",
            },
        )
        out = _render_tekton_task(step, "", "s", ["bash", "/run.sh"], tc, cs, env)
        assert "oc exec" in out

    def test_delete(self, env, tc, cs):
        step = Step(
            name="s",
            type="command",
            config={
                "command": "delete",
                "selector": "app=x",
            },
        )
        out = _render_tekton_task(step, "", "s", [], tc, cs, env)
        assert "app=x" in out

    def test_delete_all(self, env, tc, cs):
        step = Step(
            name="s",
            type="command",
            config={
                "command": "delete-all",
                "configmap_name": "cm",
            },
        )
        out = _render_tekton_task(step, "", "s", [], tc, cs, env)
        assert "oc delete" in out

    def test_unknown_command_raises(self, env, tc, cs):
        step = Step(name="s", type="command", config={"command": "bad"})
        with pytest.raises(ValueError, match="Unknown command"):
            _render_tekton_task(step, "", "s", [], tc, cs, env)


# -- compute_setup_steps / compute_teardown_steps -----------------------------


class TestComputeSteps:
    def test_setup_step_names(self, env, tc, cs):
        steps = compute_setup_steps(
            [_test()],
            tc,
            cs,
            env,
            "cluster/ocp-test.yaml",
            "examples/minimal",
            "# agg",
        )
        names = [s.name for s in steps]
        assert names == [
            "configmap",
            "apply-configmap",
            "builder-pod",
            "create-builder",
            "build",
        ]

    def test_setup_configmap_has_source(self, env, tc, cs):
        steps = compute_setup_steps(
            [_test()],
            tc,
            cs,
            env,
            "cluster/ocp-test.yaml",
            "examples/minimal",
            "# agg",
        )
        cm = next(s for s in steps if s.name == "configmap")
        assert "t_test.go" in cm.content
        assert "build.sh" in cm.content

    def test_teardown_step_names(self, env, tc, cs):
        steps = compute_teardown_steps(tc, cs, env)
        names = [s.name for s in steps]
        assert names == [
            "aggregator-pod",
            "create-aggregator",
            "aggregate",
            "cleanup",
        ]

    def test_teardown_cleanup_has_configmap(self, env, tc, cs):
        steps = compute_teardown_steps(tc, cs, env)
        cleanup = next(s for s in steps if s.name == "cleanup")
        assert cleanup.config["configmap_name"] == "uat-cm"

    def test_teardown_all_on_error_run(self, env, tc, cs):
        steps = compute_teardown_steps(tc, cs, env)
        for s in steps:
            assert s.config.get("onError") == "run"
