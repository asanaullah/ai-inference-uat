import pytest

from src.common import (
    _deep_merge_spec,
    _register_service,
    _yaml_quote,
    add_ephemeral_steps,
    add_persistent_steps,
    build_command,
    create_jinja_env,
    parse_k8s_quantity,
    sanitize_node_name,
    validate_manifest,
    validate_node_resources,
)
from src.models import (
    CommandConfig,
    DAGStep,
    LoadedTest,
    NodeSpec,
    ServiceConfig,
    TestSpec,
    ToolConfig,
)

# -- validate_manifest --------------------------------------------------------


class TestValidateManifest:
    def test_valid(self):
        validate_manifest("apiVersion: v1\nkind: Pod\nmetadata:\n  name: x\n")

    def test_multi_doc(self):
        validate_manifest(
            "apiVersion: v1\nkind: Pod\nmetadata:\n  name: a\n"
            "---\n"
            "apiVersion: v1\nkind: Service\nmetadata:\n  name: b\n"
        )

    def test_generate_name(self):
        validate_manifest(
            "apiVersion: v1\nkind: PipelineRun\nmetadata:\n  generateName: x-\n"
        )

    def test_missing_api_version(self):
        with pytest.raises(ValueError, match="apiVersion"):
            validate_manifest("kind: Pod\nmetadata:\n  name: x\n")

    def test_missing_kind(self):
        with pytest.raises(ValueError, match="kind"):
            validate_manifest("apiVersion: v1\nmetadata:\n  name: x\n")

    def test_missing_name(self):
        with pytest.raises(ValueError, match="metadata.name"):
            validate_manifest("apiVersion: v1\nkind: Pod\nmetadata: {}\n")

    def test_not_a_mapping(self):
        with pytest.raises(TypeError, match="not a mapping"):
            validate_manifest("- item\n")

    def test_null_doc_skipped(self):
        validate_manifest("---\napiVersion: v1\nkind: Pod\nmetadata:\n  name: x\n---\n")


# -- _yaml_quote --------------------------------------------------------------


class TestYamlQuote:
    def test_plain(self):
        assert _yaml_quote("hello") == "hello"

    def test_empty(self):
        assert _yaml_quote("") == '""'

    def test_special_chars(self):
        for c in ":{}[],'|>&*#?!%@":
            assert _yaml_quote(f"a{c}b").startswith('"')

    def test_leading_space(self):
        assert _yaml_quote(" x") == '" x"'

    def test_trailing_space(self):
        assert _yaml_quote("x ") == '"x "'

    def test_bare_integer(self):
        assert _yaml_quote("8000") == '"8000"'

    def test_bare_float(self):
        assert _yaml_quote("3.14") == '"3.14"'

    def test_negative_number(self):
        assert _yaml_quote("-1") == '"-1"'

    def test_yaml11_booleans(self):
        for b in ("true", "false", "yes", "no", "on", "off"):
            assert _yaml_quote(b) == f'"{b}"'
            assert _yaml_quote(b.upper()) == f'"{b.upper()}"'
            assert _yaml_quote(b.capitalize()) == f'"{b.capitalize()}"'

    def test_null_variants(self):
        for n in ("null", "Null", "NULL", "~"):
            assert _yaml_quote(n) == f'"{n}"'

    def test_non_numeric_string(self):
        assert _yaml_quote("hello-world") == "hello-world"


# -- sanitize_node_name -------------------------------------------------------


class TestSanitizeNodeName:
    def test_short_valid(self):
        assert sanitize_node_name("wrk-4") == "wrk-4"

    def test_dots_replaced(self):
        assert sanitize_node_name("node.example.com") == "node-example-com"

    def test_fqdn_long(self):
        result = sanitize_node_name("ip-10-0-1-42.ec2.internal")
        assert len(result) <= 17
        assert result.startswith("ip-10-0-1-42-")
        assert len(result.split("-")[-1]) == 4

    def test_uppercase(self):
        assert sanitize_node_name("Node-A") == "node-a"

    def test_short_16_chars(self):
        name = "a" * 16
        assert sanitize_node_name(name) == name

    def test_17_chars_hashed(self):
        name = "a" * 17
        result = sanitize_node_name(name)
        assert len(result) == 17
        assert result[:12] == "a" * 12


# -- build_command ------------------------------------------------------------


class TestBuildCommand:
    def test_args_only(self):
        assert build_command(["run", "test"], {}) == ["run", "test"]

    def test_flags(self):
        assert build_command(["cmd"], {"port": 8000, "verbose": True}) == [
            "cmd",
            "--port=8000",
            "--verbose=True",
        ]

    def test_empty(self):
        assert build_command([], {}) == []


# -- Jinja2 filters -----------------------------------------------------------


class TestJinjaFilters:
    @pytest.fixture()
    def env(self):
        return create_jinja_env("templates")

    def test_to_yaml(self, env):
        t = env.from_string("{{ data | to_yaml }}")
        assert t.render(data={"a": 1}) == "a: 1"

    def test_to_json(self, env):
        t = env.from_string("{{ data | toJson }}")
        assert t.render(data=[1, 2]) == "[1, 2]"

    def test_shell_join(self, env):
        t = env.from_string("{{ args | shell_join }}")
        assert t.render(args=["echo", "hello world"]) == "echo 'hello world'"


# -- _deep_merge_spec ---------------------------------------------------------


class TestDeepMergeSpec:
    def test_simple_override(self):
        base = {"serverConfig": {"model": "a"}, "source": {"ginkgo": "t.go"}}
        override = {"serverConfig": {"model": "b"}}
        result = _deep_merge_spec(base, override)
        assert result["serverConfig"]["model"] == "b"
        assert result["source"] == {"ginkgo": "t.go"}

    def test_dag_by_name(self):
        base = {
            "dag": [
                {"name": "server", "image": "old"},
                {"name": "client", "image": "img"},
            ]
        }
        override = {"dag": {"server": {"image": "new"}}}
        result = _deep_merge_spec(base, override)
        assert len(result["dag"]) == 2
        server = next(d for d in result["dag"] if d["name"] == "server")
        assert server["image"] == "new"
        client = next(d for d in result["dag"] if d["name"] == "client")
        assert client["image"] == "img"

    def test_nested_merge(self):
        base = {"a": {"b": {"c": 1, "d": 2}}}
        override = {"a": {"b": {"c": 3}}}
        result = _deep_merge_spec(base, override)
        assert result["a"]["b"]["c"] == 3
        assert result["a"]["b"]["d"] == 2

    def test_non_dict_replacement(self):
        base = {"x": [1, 2, 3]}
        override = {"x": [4, 5]}
        result = _deep_merge_spec(base, override)
        assert result["x"] == [4, 5]


# -- parse_k8s_quantity -------------------------------------------------------


class TestParseK8sQuantity:
    def test_plain_integer(self):
        assert parse_k8s_quantity(4) == 4.0

    def test_plain_string(self):
        assert parse_k8s_quantity("4") == 4.0

    def test_millicores(self):
        assert parse_k8s_quantity("500m") == 0.5

    def test_ki(self):
        assert parse_k8s_quantity("1Ki") == 1024.0

    def test_mi(self):
        assert parse_k8s_quantity("1Mi") == 1024 * 1024

    def test_gi(self):
        assert parse_k8s_quantity("2Gi") == 2 * 1024**3

    def test_ti(self):
        assert parse_k8s_quantity("1Ti") == 1024**4

    def test_decimal_with_binary_suffix(self):
        assert parse_k8s_quantity("1.5Gi") == 1.5 * 1024**3

    def test_decimal_nano(self):
        assert parse_k8s_quantity("100n") == pytest.approx(100e-9)

    def test_decimal_micro(self):
        assert parse_k8s_quantity("100u") == pytest.approx(100e-6)

    def test_decimal_kilo(self):
        assert parse_k8s_quantity("1k") == 1000.0

    def test_decimal_mega(self):
        assert parse_k8s_quantity("2M") == 2e6

    def test_decimal_giga(self):
        assert parse_k8s_quantity("1G") == 1e9

    def test_decimal_tera(self):
        assert parse_k8s_quantity("1T") == 1e12

    def test_decimal_plain(self):
        assert parse_k8s_quantity("0.5") == 0.5

    def test_empty_string(self):
        assert parse_k8s_quantity("") == 0.0


# -- _register_service --------------------------------------------------------


class TestRegisterService:
    def _make_dag_step(self, svc_name="server", port=8080):
        return DAGStep(
            name="runner",
            image="test:latest",
            service=ServiceConfig(enabled=True, name=svc_name, port=port),
        )

    def test_disabled_service_returns_empty(self):
        step = DAGStep(name="runner", image="test:latest")
        services: dict = {}
        assert _register_service(step, "pfx", services) == ""
        assert services == {}

    def test_service_name_includes_prefix(self):
        step = self._make_dag_step()
        services: dict = {}
        name = _register_service(step, "1-mytest-wrk-0", services)
        assert name == "svc-1-mytest-wrk-0-server"
        assert services["server"]["name"] == name
        assert services["server"]["url"] == f"http://{name}:8080"

    def test_sweep_entries_get_unique_service_names(self):
        step = self._make_dag_step()
        services: dict = {}
        name_a = _register_service(step, "1-mytest-wrk-0-sweep-a", services)
        name_b = _register_service(step, "1-mytest-wrk-0-sweep-b", services)
        assert name_a != name_b
        assert "sweep-a" in name_a
        assert "sweep-b" in name_b


# -- add_persistent_steps / add_ephemeral_steps --------------------------------

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


def _loaded_test(name="mytest", test_id="1", timeout=None):
    spec = TestSpec(
        source={"ginkgo": "t.go"},
        dag=[{"name": "run", "image": "img", "labelFilter": "pass-fail"}],
    )
    return LoadedTest(
        name=name,
        spec=spec,
        go_source="x",
        on_failure="continue",
        timeout=timeout,
        test_id=test_id,
    )


class TestAddPersistentSteps:
    @pytest.fixture()
    def env(self):
        return create_jinja_env("templates")

    @pytest.fixture()
    def tc(self):
        return ToolConfig(**TC_DATA)

    def test_generates_two_steps(self, env, tc):
        dag_step = DAGStep(name="server", image="img:latest")
        steps = []
        add_persistent_steps(
            steps,
            dag_step,
            "s-1-t-wrk-0",
            "1-t-wrk-0",
            "wrk-0",
            "wrk-0",
            _loaded_test(),
            tc,
            "ns",
            "pvc",
            "results",
            {},
            env,
            "node",
        )
        assert len(steps) == 2
        assert steps[0].type == "generate"
        assert steps[1].type == "command"
        assert steps[1].config["probe"] == "wait-ready"

    def test_service_name_in_config(self, env, tc):
        dag_step = DAGStep(
            name="server",
            image="img:latest",
            service=ServiceConfig(enabled=True, name="server", port=8080),
        )
        steps = []
        add_persistent_steps(
            steps,
            dag_step,
            "s-1-t-wrk-0",
            "1-t-wrk-0",
            "wrk-0",
            "wrk-0",
            _loaded_test(),
            tc,
            "ns",
            "pvc",
            "results",
            {},
            env,
            "node",
        )
        assert steps[0].config["service_name"] == "svc-1-t-wrk-0-server"


class TestAddEphemeralSteps:
    @pytest.fixture()
    def env(self):
        return create_jinja_env("templates")

    @pytest.fixture()
    def tc(self):
        return ToolConfig(**TC_DATA)

    def test_no_sweep_produces_three_steps(self, env, tc):
        dag_step = DAGStep(
            name="runner",
            image="img:latest",
            command=CommandConfig(args=["./run"]),
        )
        steps = []
        add_ephemeral_steps(
            steps,
            dag_step,
            "s-1-t-wrk-0",
            "1-t-wrk-0",
            "wrk-0",
            "wrk-0",
            _loaded_test(),
            tc,
            "ns",
            "pvc",
            "results",
            {},
            env,
            "node",
        )
        assert len(steps) == 3
        assert steps[0].type == "generate"
        assert steps[1].type == "command"
        assert steps[1].config["probe"] == "poll-completed"
        assert steps[2].config["command"] == "delete"

    def test_sweep_produces_steps_per_entry(self, env, tc):
        dag_step = DAGStep(
            name="bench",
            image="img:latest",
            parameterSweep={
                "baseCommand": {"args": ["./bench"], "flags": {"--threads": "1"}},
                "entries": [
                    {"id": "t1", "flags": {"--threads": "1"}},
                    {"id": "t4", "flags": {"--threads": "4"}},
                ],
            },
        )
        steps = []
        add_ephemeral_steps(
            steps,
            dag_step,
            "s-1-t-wrk-0",
            "1-t-wrk-0",
            "wrk-0",
            "wrk-0",
            _loaded_test(),
            tc,
            "ns",
            "pvc",
            "results",
            {},
            env,
            "node",
        )
        assert len(steps) == 6
        res_names = [s.resource_name for s in steps]
        assert "1-t-wrk-0-bench-t1" in res_names
        assert "1-t-wrk-0-bench-t4" in res_names

    def test_sweep_service_names_unique(self, env, tc):
        dag_step = DAGStep(
            name="bench",
            image="img:latest",
            service=ServiceConfig(enabled=True, name="server", port=9090),
            parameterSweep={
                "baseCommand": {"args": ["./bench"]},
                "entries": [
                    {"id": "a"},
                    {"id": "b"},
                ],
            },
        )
        steps = []
        add_ephemeral_steps(
            steps,
            dag_step,
            "s-1-t-wrk-0",
            "1-t-wrk-0",
            "wrk-0",
            "wrk-0",
            _loaded_test(),
            tc,
            "ns",
            "pvc",
            "results",
            {},
            env,
            "node",
        )
        svc_names = [
            s.config["service_name"] for s in steps if "service_name" in s.config
        ]
        assert len(svc_names) == 2
        assert svc_names[0] != svc_names[1]


# -- validate_node_resources --------------------------------------------------


class TestValidateNodeResources:
    @pytest.fixture()
    def env(self):
        return create_jinja_env("templates")

    def _node(self, **extra_sanity):
        sanity = {"gpuCount": 4, **extra_sanity}
        return NodeSpec(
            name="wrk-1",
            componentValidation={"sanity": sanity},
        )

    def _test(self, dag):
        spec = TestSpec(source={"ginkgo": "t.go"}, dag=dag)
        return LoadedTest(name="t", spec=spec, go_source="x", test_id="1")

    def test_passes_when_within_capacity(self, env):
        dag = [
            {
                "name": "run",
                "image": "img",
                "resources": {"requests": {"nvidia.com/gpu": "2"}},
                "labelFilter": "pass-fail",
            }
        ]
        validate_node_resources(
            self._test(dag), self._node(**{"nvidia.com/gpu": 4}), env
        )

    def test_fails_when_exceeds_capacity(self, env):
        dag = [
            {
                "name": "run",
                "image": "img",
                "resources": {"requests": {"nvidia.com/gpu": "6"}},
                "labelFilter": "pass-fail",
            }
        ]
        with pytest.raises(ValueError, match="exceeds capacity"):
            validate_node_resources(
                self._test(dag), self._node(**{"nvidia.com/gpu": 4}), env
            )

    def test_skips_unknown_resource(self, env):
        dag = [
            {
                "name": "run",
                "image": "img",
                "resources": {"requests": {"custom/resource": "100"}},
                "labelFilter": "pass-fail",
            }
        ]
        validate_node_resources(self._test(dag), self._node(), env)

    def test_peak_demand_persistent_plus_ephemeral(self, env):
        dag = [
            {
                "name": "server",
                "image": "img",
                "persistsThroughSweep": True,
                "resources": {"requests": {"nvidia.com/gpu": "2"}},
            },
            {
                "name": "client",
                "image": "img",
                "resources": {"requests": {"nvidia.com/gpu": "3"}},
                "labelFilter": "pass-fail",
            },
        ]
        with pytest.raises(ValueError, match="exceeds capacity"):
            validate_node_resources(
                self._test(dag), self._node(**{"nvidia.com/gpu": 4}), env
            )
