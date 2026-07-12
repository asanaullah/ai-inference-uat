import pytest
from pydantic import ValidationError

from src.models import (
    ClusterTest,
    DAGStep,
    NodeSpec,
    StepsFile,
    Test,
    TestSuite,
    ToolConfig,
    _validate_section,
)

# -- Fixtures -----------------------------------------------------------------

TOOL_CONFIG_DATA = {
    "oseCLIImage": "ose:latest",
    "builderImage": "golang:1.25",
    "aggregatorImage": "python:3-slim",
    "configmapName": "uat-cm",
    "builderPodName": "builder",
    "aggregatorPodName": "aggregator",
    "nodeSelectorKey": "kubernetes.io/hostname",
    "managedByLabel": "uat",
}

CLUSTER_SPEC_DATA = {
    "nodes": [
        {
            "name": "wrk-1",
            "componentValidation": {"sanity": {"gpuCount": 4}},
        }
    ],
    "namespace": "ns",
    "storage": {"pvc": "pvc", "basePath": "results"},
}


def _dag_step(**overrides):
    base = {"name": "step", "image": "img:latest"}
    return DAGStep(**(base | overrides))


# -- ToolConfig ---------------------------------------------------------------


class TestToolConfig:
    def test_from_camel_case(self):
        tc = ToolConfig(**TOOL_CONFIG_DATA)
        assert tc.ose_cli_image == "ose:latest"
        assert tc.configmap_name == "uat-cm"

    def test_from_snake_case(self):
        tc = ToolConfig(
            ose_cli_image="a",
            builder_image="b",
            aggregator_image="c",
            configmap_name="d",
            builder_pod_name="e",
            aggregator_pod_name="f",
            node_selector_key="g",
            managed_by_label="h",
        )
        assert tc.ose_cli_image == "a"

    def test_defaults(self):
        tc = ToolConfig(**TOOL_CONFIG_DATA)
        assert tc.builder_timeout == "300s"
        assert tc.aggregator_timeout == "120s"
        assert tc.deploy_timeout == "600s"
        assert tc.test_timeout == "600s"
        assert tc.pipeline_timeout == "2h"
        assert tc.finally_timeout == "15m"

    def test_missing_required_field(self):
        with pytest.raises(ValidationError):
            ToolConfig(
                **{k: v for k, v in TOOL_CONFIG_DATA.items() if k != "builderImage"}
            )


# -- TestSuite ----------------------------------------------------------------


class TestTestSuite:
    def test_minimal(self):
        ts = TestSuite(**{"spec": {"tests": {"node": ["a"]}}})
        assert ts.spec.tests.node == ["a"]
        assert ts.spec.tests.cluster == []
        assert ts.spec.execution.stop_on_failure is False

    def test_stop_on_failure(self):
        ts = TestSuite(
            **{"spec": {"tests": {"node": []}, "execution": {"stopOnFailure": True}}}
        )
        assert ts.spec.execution.stop_on_failure is True

    def test_missing_tests(self):
        with pytest.raises(ValidationError):
            TestSuite(**{"spec": {}})


# -- ClusterTest / NodeSpec ---------------------------------------------------


class TestClusterTest:
    def test_parse(self):
        ct = ClusterTest(**{"spec": CLUSTER_SPEC_DATA})
        assert ct.spec.namespace == "ns"
        assert ct.spec.nodes[0].name == "wrk-1"
        assert ct.spec.nodes[0].component_validation.sanity.gpu_count == 4

    def test_extra_fields_allowed(self):
        node_data = {
            "name": "n",
            "componentValidation": {
                "sanity": {"gpuCount": 2, "gpuModel": "A100"},
                "custom": "value",
            },
        }
        ns = NodeSpec(**node_data)
        assert ns.component_validation.sanity.gpu_count == 2
        assert ns.component_validation.sanity.model_extra["gpuModel"] == "A100"


# -- Test / DAGStep -----------------------------------------------------------


class TestTestDefinition:
    def test_minimal_test(self):
        t = Test(
            **{
                "spec": {
                    "source": {"ginkgo": "t.go", "goMod": "go.mod", "goSum": "go.sum"},
                    "dag": [{"name": "run", "image": "img"}],
                }
            }
        )
        assert t.spec.dag[0].name == "run"
        assert t.spec.requirements.gpu is False

    def test_dag_defaults(self):
        d = _dag_step()
        assert d.persists_through_sweep is False
        assert d.privileged is False
        assert d.service.enabled is False
        assert d.service.headless is True
        assert d.parameter_sweep is None

    def test_dag_with_sweep(self):
        d = _dag_step(
            parameterSweep={
                "baseCommand": {"args": ["run"], "flags": {"k": "v"}},
                "entries": [{"id": "e1"}],
            }
        )
        assert d.parameter_sweep.entries[0].id == "e1"
        assert d.parameter_sweep.base_command.flags == {"k": "v"}

    def test_dag_missing_image(self):
        with pytest.raises(ValidationError):
            DAGStep(name="x")


# -- _validate_section --------------------------------------------------------


class TestValidateSection:
    def _gen(self, name="g", **kw):
        return {
            "name": name,
            "type": "generate",
            "content": "x",
            "config": {"output": "manifest"},
        } | kw

    def _cmd(self, name="c", **kw):
        return {
            "name": name,
            "type": "command",
            "source": [],
            "config": {"command": "apply", "probe": "none"},
        } | kw

    def test_valid(self):
        _validate_section([self._gen(), self._cmd()], "s")

    def test_duplicate_name(self):
        with pytest.raises(ValueError, match="Duplicate"):
            _validate_section([self._gen(), self._gen()], "s")

    def test_missing_name(self):
        with pytest.raises(ValueError, match="missing 'name'"):
            _validate_section([{"type": "generate"}], "s")

    def test_invalid_type(self):
        with pytest.raises(ValueError, match="invalid type"):
            _validate_section([{"name": "x", "type": "bad"}], "s")

    def test_empty_content(self):
        with pytest.raises(ValueError, match="empty content"):
            _validate_section([self._gen(content="")], "s")

    def test_invalid_command(self):
        with pytest.raises(ValueError, match="invalid command"):
            _validate_section([self._cmd(config={"command": "nope"})], "s")

    def test_invalid_probe(self):
        with pytest.raises(ValueError, match="invalid probe"):
            _validate_section(
                [self._cmd(config={"command": "apply", "probe": "bad"})], "s"
            )

    def test_bad_source_ref(self):
        with pytest.raises(ValueError, match="not a preceding generate"):
            _validate_section([self._cmd(source=["missing"])], "s")

    def test_source_ref_to_preceding_gen(self):
        _validate_section([self._gen("g1"), self._cmd(source=["g1"])], "s")


# -- StepsFile ----------------------------------------------------------------


class TestStepsFile:
    def _valid_data(self):
        return {
            "metadata": {
                "toolConfig": TOOL_CONFIG_DATA,
                "clusterSpec": CLUSTER_SPEC_DATA,
                "stopOnFailure": False,
            },
            "setup": [
                {
                    "name": "cm",
                    "type": "generate",
                    "content": "x",
                    "config": {"output": "manifest"},
                },
                {
                    "name": "apply-cm",
                    "type": "command",
                    "source": ["cm"],
                    "config": {"command": "apply", "probe": "none"},
                },
            ],
            "nodes": {},
            "teardown": [],
        }

    def test_valid(self):
        StepsFile(**self._valid_data())

    def test_missing_metadata_key(self):
        data = self._valid_data()
        del data["metadata"]["stopOnFailure"]
        with pytest.raises(ValidationError, match="stopOnFailure"):
            StepsFile(**data)

    def test_invalid_tool_config_in_metadata(self):
        data = self._valid_data()
        data["metadata"]["toolConfig"] = {"bad": "data"}
        with pytest.raises(ValidationError):
            StepsFile(**data)
