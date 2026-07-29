# Assisted by Claude Opus 4.6
import pytest

from src.cluster import compute_cluster_steps, _filter_nodes
from src.common import create_jinja_env
from src.models import LoadedTest, NodeSpec, Placement, TestSpec, ToolConfig


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


def _node(name="wrk-1", gpu_count=4, **extra_sanity):
    sanity = {"gpuCount": gpu_count, **extra_sanity}
    return NodeSpec(
        name=name,
        componentValidation={"sanity": sanity},
    )


def _test(
    name="t",
    dag=None,
    on_failure="continue",
    timeout=None,
    test_id="1",
    placement=None,
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
        scope="cluster",
        placement=placement,
    )


class TestFilterNodes:
    def test_no_requirements_returns_all(self):
        nodes = [_node("a"), _node("b")]
        assert _filter_nodes(nodes, {}) == nodes

    def test_numeric_minimum(self):
        nodes = [_node("a", gpu_count=4), _node("b", gpu_count=2)]
        result = _filter_nodes(nodes, {"gpuCount": 4})
        assert len(result) == 1
        assert result[0].name == "a"

    def test_string_exact_match(self):
        nodes = [
            _node("a", gpuModel="H100"),
            _node("b", gpuModel="A100"),
        ]
        result = _filter_nodes(nodes, {"gpuModel": "H100"})
        assert len(result) == 1
        assert result[0].name == "a"

    def test_missing_field_excludes_node(self):
        nodes = [_node("a", gpu_count=4)]
        result = _filter_nodes(nodes, {"nvlink": True})
        assert result == []


class TestComputeClusterSteps:
    @pytest.fixture()
    def env(self):
        return create_jinja_env("templates")

    @pytest.fixture()
    def tc(self):
        return ToolConfig(**TC_DATA)

    def test_single_set_random(self, env, tc):
        nodes = [_node("wrk-1"), _node("wrk-2")]
        steps, mappings = compute_cluster_steps(
            _test(), tc, "ns", "pvc", "results", env, nodes=nodes
        )
        names = [s.name for s in steps]
        assert any("run" in n for n in names)
        assert any("finally-teardown" in n for n in names)
        assert len(mappings) == 1

    def test_single_set_no_set_segment(self, env, tc):
        nodes = [_node("wrk-1")]
        steps, _ = compute_cluster_steps(
            _test(), tc, "ns", "pvc", "results", env, nodes=nodes
        )
        names = [s.name for s in steps]
        assert "1-t-run" in names
        assert "1-t-finally-teardown" in names
        assert all("set" not in n for n in names)

    def test_multi_set_has_set_segment(self, env, tc):
        nodes = [_node("wrk-1"), _node("wrk-2")]
        placement = Placement(
            setSelection="all", setCutoff=0, setSize=1, setType="combination"
        )
        steps, mappings = compute_cluster_steps(
            _test(placement=placement), tc, "ns", "pvc", "results", env, nodes=nodes
        )
        names = [s.name for s in steps]
        assert "1-t-set0-run" in names
        assert "1-t-set1-run" in names
        assert len(mappings) == 2

    def test_set_size_validation(self, env, tc):
        nodes = [_node("wrk-1")]
        placement = Placement(setSize=2)
        with pytest.raises(ValueError, match="setSize.*exceeds"):
            compute_cluster_steps(
                _test(placement=placement), tc, "ns", "pvc", "results", env, nodes=nodes
            )

    def test_set_size_dag_count_mismatch(self, env, tc):
        nodes = [_node("wrk-1"), _node("wrk-2")]
        placement = Placement(setSize=2, setSelection="random")
        dag = [{"name": "run", "image": "img", "labelFilter": "pass-fail"}]
        with pytest.raises(ValueError, match="setSize.*DAG steps"):
            compute_cluster_steps(
                _test(dag=dag, placement=placement),
                tc,
                "ns",
                "pvc",
                "results",
                env,
                nodes=nodes,
            )

    def test_multi_node_set_pins_dag_steps(self, env, tc):
        nodes = [_node("wrk-1"), _node("wrk-2")]
        placement = Placement(setSize=2, setSelection="random")
        dag = [
            {
                "name": "server",
                "image": "img",
                "persistsThroughSweep": True,
                "service": {"enabled": True, "port": 8000, "name": "server"},
            },
            {"name": "client", "image": "img", "labelFilter": "pass-fail"},
        ]
        steps, mappings = compute_cluster_steps(
            _test(dag=dag, placement=placement),
            tc,
            "ns",
            "pvc",
            "results",
            env,
            nodes=nodes,
        )
        gen_steps = [s for s in steps if s.type == "generate"]
        server_gen = [s for s in gen_steps if "server" in s.name][0]
        client_gen = [s for s in gen_steps if "client" in s.name][0]
        assert "wrk-1" in server_gen.content or "wrk-2" in server_gen.content
        assert "wrk-1" in client_gen.content or "wrk-2" in client_gen.content

    def test_on_failure_propagated(self, env, tc):
        nodes = [_node("wrk-1")]
        for policy in ("continue", "skipTest", "abort"):
            steps, _ = compute_cluster_steps(
                _test(on_failure=policy), tc, "ns", "pvc", "results", env, nodes=nodes
            )
            for s in steps:
                assert s.on_failure == policy

    def test_scope_is_cluster(self, env, tc):
        nodes = [_node("wrk-1")]
        steps, _ = compute_cluster_steps(
            _test(), tc, "ns", "pvc", "results", env, nodes=nodes
        )
        for s in steps:
            assert s.scope == "cluster"

    def test_no_eligible_nodes_returns_empty(self, env, tc):
        nodes = [_node("wrk-1", gpu_count=0)]
        placement = Placement(setRequirements={"gpuCount": 4})
        steps, mappings = compute_cluster_steps(
            _test(placement=placement), tc, "ns", "pvc", "results", env, nodes=nodes
        )
        assert steps == []
        assert mappings == {}

    def test_set_cutoff_limits_sets(self, env, tc):
        nodes = [_node("a"), _node("b"), _node("c")]
        placement = Placement(setSelection="all", setCutoff=2, setSize=1)
        steps, mappings = compute_cluster_steps(
            _test(placement=placement), tc, "ns", "pvc", "results", env, nodes=nodes
        )
        assert len(mappings) == 2

    def test_persistent_generates_teardown(self, env, tc):
        nodes = [_node("wrk-1")]
        dag = [
            {
                "name": "server",
                "image": "img",
                "persistsThroughSweep": True,
                "service": {"enabled": True, "port": 8000, "name": "server"},
            },
            {"name": "run", "image": "img", "labelFilter": "pass-fail"},
        ]
        steps, _ = compute_cluster_steps(
            _test(dag=dag), tc, "ns", "pvc", "results", env, nodes=nodes
        )
        names = [s.name for s in steps]
        assert "1-t-teardown" in names
        assert "1-t-finally-teardown" in names

    def test_resource_validation_failure(self, env, tc):
        nodes = [_node("wrk-1", **{"nvidia.com/gpu": 2})]
        dag = [
            {
                "name": "run",
                "image": "img",
                "labelFilter": "pass-fail",
                "resources": {"requests": {"nvidia.com/gpu": "4"}},
            }
        ]
        with pytest.raises(ValueError, match="demand.*exceeds capacity"):
            compute_cluster_steps(
                _test(dag=dag), tc, "ns", "pvc", "results", env, nodes=nodes
            )

    def test_chain_key_multi_set(self, env, tc):
        nodes = [_node("wrk-1"), _node("wrk-2")]
        placement = Placement(setSelection="all", setCutoff=0, setSize=1)
        steps, _ = compute_cluster_steps(
            _test(placement=placement), tc, "ns", "pvc", "results", env, nodes=nodes
        )
        chain_keys = {s.node for s in steps}
        assert "set0" in chain_keys
        assert "set1" in chain_keys

    def test_chain_key_single_set(self, env, tc):
        nodes = [_node("wrk-1")]
        steps, _ = compute_cluster_steps(
            _test(), tc, "ns", "pvc", "results", env, nodes=nodes
        )
        chain_keys = {s.node for s in steps}
        assert chain_keys == {""}
