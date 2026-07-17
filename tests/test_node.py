import pytest

from src.common import create_jinja_env
from src.models import LoadedTest, NodeSpec, TestSpec, ToolConfig
from src.node import compute_node_steps, node_meets_requirements


# -- Fixtures -----------------------------------------------------------------

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


def _node(gpu_count=4):
    return NodeSpec(
        name="wrk-1",
        componentValidation={"sanity": {"gpuCount": gpu_count}},
    )


def _test(
    name="t", gpu=False, dag=None, on_failure="continue", timeout=None, test_id="1"
):
    dag = dag or [{"name": "run", "image": "img", "labelFilter": "pass-fail"}]
    spec = TestSpec(
        source={"ginkgo": "t.go", "goMod": "go.mod", "goSum": "go.sum"},
        dag=dag,
        requirements={"gpu": gpu},
    )
    return LoadedTest(
        name=name,
        spec=spec,
        go_source="x",
        go_mod="x",
        go_sum="x",
        on_failure=on_failure,
        timeout=timeout,
        test_id=test_id,
    )


# -- node_meets_requirements -------------------------------------------------


class TestNodeMeetsRequirements:
    def test_no_gpu_required(self):
        assert node_meets_requirements(_test().spec.requirements, _node(0))

    def test_gpu_required_has_gpu(self):
        assert node_meets_requirements(_test(gpu=True).spec.requirements, _node(4))

    def test_gpu_required_no_gpu(self):
        assert not node_meets_requirements(_test(gpu=True).spec.requirements, _node(0))


# -- compute_node_steps -------------------------------------------------------


class TestComputeNodeSteps:
    @pytest.fixture()
    def env(self):
        return create_jinja_env("templates")

    @pytest.fixture()
    def tc(self):
        return ToolConfig(**TC_DATA)

    def test_simple_test(self, env, tc):
        steps = compute_node_steps(
            _node(),
            _test(),
            tc,
            "ns",
            "pvc",
            "results",
            env,
        )
        names = [s.name for s in steps]
        assert "1-t-wrk-1-run" in names
        assert "1-t-wrk-1-cleanup-run" in names
        assert "1-t-wrk-1-finally-teardown" in names

    def test_skips_gpu_test(self, env, tc):
        steps = compute_node_steps(
            _node(0),
            _test(gpu=True),
            tc,
            "ns",
            "pvc",
            "results",
            env,
        )
        assert steps == []

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
        steps = compute_node_steps(
            _node(),
            _test(dag=dag),
            tc,
            "ns",
            "pvc",
            "results",
            env,
        )
        names = [s.name for s in steps]
        assert "1-t-wrk-1-teardown" in names
        assert "1-t-wrk-1-finally-teardown" in names

    def test_on_failure_propagated(self, env, tc):
        for policy in ("continue", "skipTest", "abort"):
            steps = compute_node_steps(
                _node(),
                _test(on_failure=policy),
                tc,
                "ns",
                "pvc",
                "results",
                env,
            )
            for s in steps:
                assert s.on_failure == policy
                assert "onError" not in s.config

    def test_timeout_override(self, env, tc):
        steps = compute_node_steps(
            _node(),
            _test(timeout="1200s"),
            tc,
            "ns",
            "pvc",
            "results",
            env,
        )
        run_steps = [
            s
            for s in steps
            if s.type == "command" and s.config.get("probe") == "poll-completed"
        ]
        assert run_steps
        for s in run_steps:
            assert s.config["timeout"] == "1200s"

    def test_timeout_default_fallback(self, env, tc):
        steps = compute_node_steps(
            _node(),
            _test(),
            tc,
            "ns",
            "pvc",
            "results",
            env,
        )
        run_steps = [
            s
            for s in steps
            if s.type == "command" and s.config.get("probe") == "poll-completed"
        ]
        assert run_steps
        for s in run_steps:
            assert s.config["timeout"] == "600s"

    def test_finally_step_flag(self, env, tc):
        steps = compute_node_steps(
            _node(),
            _test(),
            tc,
            "ns",
            "pvc",
            "results",
            env,
        )
        finally_steps = [s for s in steps if s.finally_step]
        assert len(finally_steps) == 1
        assert finally_steps[0].name == "1-t-wrk-1-finally-teardown"
        non_finally = [s for s in steps if not s.finally_step]
        assert all(not s.finally_step for s in non_finally)

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
        steps = compute_node_steps(
            _node(),
            _test(dag=dag),
            tc,
            "ns",
            "pvc",
            "results",
            env,
        )
        gen_names = [s.name for s in steps if s.type == "generate"]
        assert "1-t-wrk-1-bench-e1" in gen_names
        assert "1-t-wrk-1-bench-e2" in gen_names

    def test_persistent_with_sweep_rejected(self, env, tc):
        dag = [
            {
                "name": "server",
                "image": "img",
                "persistsThroughSweep": True,
                "parameterSweep": {
                    "baseCommand": {"args": ["run"], "flags": {}},
                    "entries": [{"id": "e1"}],
                },
            }
        ]
        with pytest.raises(ValueError, match="persistsThroughSweep.*parameterSweep"):
            compute_node_steps(
                _node(),
                _test(dag=dag),
                tc,
                "ns",
                "pvc",
                "results",
                env,
            )
