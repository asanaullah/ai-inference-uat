# Assisted by Claude Opus 4.6
import pytest

from src.common import create_jinja_env
from src.models import LoadedTest, TestSpec, ToolConfig
from src.project import compute_project_steps


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


def _test(
    name="t",
    dag=None,
    on_failure="continue",
    timeout=None,
    test_id="1",
):
    dag = dag or [{"name": "run", "image": "img", "labelFilter": "pass-fail"}]
    spec = TestSpec(
        source={"ginkgo": "t.go"},
        dag=dag,
    )
    return LoadedTest(
        name=name,
        spec=spec,
        go_source="x",
        on_failure=on_failure,
        timeout=timeout,
        test_id=test_id,
        scope="project",
    )


class TestComputeProjectSteps:
    @pytest.fixture()
    def env(self):
        return create_jinja_env("templates")

    @pytest.fixture()
    def tc(self):
        return ToolConfig(**TC_DATA)

    def test_simple_test(self, env, tc):
        steps = compute_project_steps(_test(), tc, "ns", "pvc", "results", env)
        names = [s.name for s in steps]
        assert "1-t-run" in names
        assert "1-t-cleanup-run" in names
        assert "1-t-finally-teardown" in names

    def test_no_node_in_names(self, env, tc):
        steps = compute_project_steps(_test(), tc, "ns", "pvc", "results", env)
        for s in steps:
            assert s.node == ""

    def test_no_node_selector_in_manifests(self, env, tc):
        steps = compute_project_steps(_test(), tc, "ns", "pvc", "results", env)
        gen_steps = [s for s in steps if s.type == "generate"]
        for s in gen_steps:
            assert "nodeSelector" not in s.content

    def test_scope_is_project(self, env, tc):
        steps = compute_project_steps(_test(), tc, "ns", "pvc", "results", env)
        for s in steps:
            assert s.scope == "project"

    def test_on_failure_propagated(self, env, tc):
        for policy in ("continue", "skipTest", "abort"):
            steps = compute_project_steps(
                _test(on_failure=policy), tc, "ns", "pvc", "results", env
            )
            for s in steps:
                assert s.on_failure == policy

    def test_persistent_generates_teardown(self, env, tc):
        dag = [
            {
                "name": "server",
                "image": "img",
                "persistsThroughSweep": True,
                "service": {"enabled": True, "port": 8000, "name": "server"},
            },
            {"name": "run", "image": "img", "labelFilter": "pass-fail"},
        ]
        steps = compute_project_steps(_test(dag=dag), tc, "ns", "pvc", "results", env)
        names = [s.name for s in steps]
        assert "1-t-teardown" in names
        assert "1-t-finally-teardown" in names

    def test_timeout_override(self, env, tc):
        steps = compute_project_steps(
            _test(timeout="1200s"), tc, "ns", "pvc", "results", env
        )
        run_steps = [
            s
            for s in steps
            if s.type == "command" and s.config.get("probe") == "poll-completed"
        ]
        assert run_steps
        for s in run_steps:
            assert s.config["timeout"] == "1200s"

    def test_sweep_creates_multiple_pods(self, env, tc):
        dag = [
            {
                "name": "bench",
                "image": "img",
                "parameterSweep": {
                    "baseCommand": {"args": ["run"], "flags": {"k": "v"}},
                    "entries": [{"id": "e1"}, {"id": "e2"}],
                },
            }
        ]
        steps = compute_project_steps(_test(dag=dag), tc, "ns", "pvc", "results", env)
        gen_names = [s.name for s in steps if s.type == "generate"]
        assert "1-t-bench-e1" in gen_names
        assert "1-t-bench-e2" in gen_names

    def test_finally_step_flag(self, env, tc):
        steps = compute_project_steps(_test(), tc, "ns", "pvc", "results", env)
        finally_steps = [s for s in steps if s.finally_step]
        assert len(finally_steps) == 1
        assert finally_steps[0].name == "1-t-finally-teardown"
