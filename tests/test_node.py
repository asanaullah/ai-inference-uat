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


def _test(name="t", gpu=False, dag=None):
    dag = dag or [{"name": "run", "image": "img", "labelFilter": "pass-fail"}]
    spec = TestSpec(
        source={"ginkgo": "t.go", "goMod": "go.mod", "goSum": "go.sum"},
        dag=dag,
        requirements={"gpu": gpu},
    )
    return LoadedTest(name=name, spec=spec, go_source="x", go_mod="x", go_sum="x")


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
            [_test()],
            tc,
            "ns",
            "pvc",
            "results",
            env,
            False,
        )
        names = [s.name for s in steps]
        assert "wrk-1-t-run" in names
        assert "run-test-t-run" in names
        assert "cleanup-t-run" in names

    def test_skips_gpu_test(self, env, tc):
        steps = compute_node_steps(
            _node(0),
            [_test(gpu=True)],
            tc,
            "ns",
            "pvc",
            "results",
            env,
            False,
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
            [_test(dag=dag)],
            tc,
            "ns",
            "pvc",
            "results",
            env,
            False,
        )
        names = [s.name for s in steps]
        assert "teardown-t" in names
        assert "finally-teardown-t" in names

    def test_stop_on_failure_propagates(self, env, tc):
        steps = compute_node_steps(
            _node(),
            [_test()],
            tc,
            "ns",
            "pvc",
            "results",
            env,
            True,
        )
        cmd_steps = [s for s in steps if s.type == "command" and "run-test" in s.name]
        assert all(s.config["onError"] == "stop" for s in cmd_steps)

    def test_continue_on_failure(self, env, tc):
        steps = compute_node_steps(
            _node(),
            [_test()],
            tc,
            "ns",
            "pvc",
            "results",
            env,
            False,
        )
        cmd_steps = [s for s in steps if s.type == "command" and "run-test" in s.name]
        assert all(s.config["onError"] == "continue" for s in cmd_steps)

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
            [_test(dag=dag)],
            tc,
            "ns",
            "pvc",
            "results",
            env,
            False,
        )
        gen_names = [s.name for s in steps if s.type == "generate"]
        assert "wrk-1-t-e1" in gen_names
        assert "wrk-1-t-e2" in gen_names
