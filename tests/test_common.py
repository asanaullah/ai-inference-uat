import pytest
import yaml

from src.common import (
    _deep_merge_spec,
    _register_service,
    _render_nested_strings,
    _yaml_quote,
    add_ephemeral_steps,
    add_persistent_steps,
    add_resource_steps,
    add_teardown_steps,
    build_command,
    create_jinja_env,
    parse_k8s_quantity,
    render_env,
    render_manifest,
    sanitize_node_name,
    validate_manifest,
    validate_node_resources,
)
from src.models import (
    CommandConfig,
    DAGStep,
    LoadedTest,
    NodeSpec,
    ResourceConfig,
    ServiceConfig,
    SidecarContainer,
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

    def test_bare_flags(self):
        assert build_command(["server"], {"enable-feature": None, "port": 8000}) == [
            "server",
            "--enable-feature",
            "--port=8000",
        ]

    def test_bare_flags_only(self):
        assert build_command(["cmd"], {"a": None, "b": None}) == [
            "cmd",
            "--a",
            "--b",
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


# -- render_env ---------------------------------------------------------------


class TestRenderEnv:
    @pytest.fixture()
    def env(self):
        return create_jinja_env("templates")

    def test_plain_value(self, env):
        result = render_env([{"name": "FOO", "value": "bar"}], {}, env)
        assert result == [{"name": "FOO", "value": "bar"}]

    def test_value_from_field_ref_passthrough(self, env):
        entry = {
            "name": "POD_IP",
            "valueFrom": {"fieldRef": {"fieldPath": "status.podIP"}},
        }
        result = render_env([entry], {}, env)
        assert len(result) == 1
        assert "value" not in result[0]
        assert result[0]["valueFrom"] == {"fieldRef": {"fieldPath": "status.podIP"}}

    def test_value_from_secret_ref_passthrough(self, env):
        entry = {
            "name": "HF_TOKEN",
            "valueFrom": {
                "secretKeyRef": {"name": "llm-d-hf-token", "key": "HF_TOKEN"}
            },
        }
        result = render_env([entry], {}, env)
        assert result[0]["valueFrom"]["secretKeyRef"]["name"] == "llm-d-hf-token"
        assert "value" not in result[0]

    def test_mixed_value_and_value_from(self, env):
        entries = [
            {"name": "PLAIN", "value": "hello"},
            {
                "name": "POD_IP",
                "valueFrom": {"fieldRef": {"fieldPath": "status.podIP"}},
            },
            {"name": "PORT", "value": "8000"},
        ]
        result = render_env(entries, {}, env)
        assert result[0] == {"name": "PLAIN", "value": "hello"}
        assert "value" not in result[1]
        assert result[1]["valueFrom"]["fieldRef"]["fieldPath"] == "status.podIP"
        assert result[2] == {"name": "PORT", "value": "8000"}

    def test_jinja_rendering_on_value_only(self, env):
        ctx = {"serverConfig": {"model": "gpt"}}
        entries = [
            {"name": "MODEL", "value": "{{ serverConfig.model }}"},
            {
                "name": "POD_IP",
                "valueFrom": {"fieldRef": {"fieldPath": "status.podIP"}},
            },
        ]
        result = render_env(entries, ctx, env)
        assert result[0]["value"] == "gpt"
        assert result[1]["valueFrom"]["fieldRef"]["fieldPath"] == "status.podIP"


# -- render_env in templates --------------------------------------------------


class TestValueFromTemplate:
    @pytest.fixture()
    def env(self):
        return create_jinja_env("templates")

    def _render_pod(self, jinja_env, template_name, env_vars, extra_labels=None):
        ctx = {
            "pod_name": "test-pod",
            "namespace": "default",
            "managed_by_label": "uat",
            "test": "test",
            "dag_step_name": "step",
            "node": None,
            "privileged": False,
            "image": "img:latest",
            "command": None,
            "env": env_vars,
            "ports": None,
            "readiness_probe": None,
            "resources": None,
            "volume_mounts": [],
            "volumes": [],
            "workspace_subpath": "ws",
            "binaries_subpath": "bin",
            "models_storage": None,
            "pvc": "pvc",
            "extra_labels": extra_labels or {},
        }
        if template_name == "test-pod.yaml.j2":
            ctx["sweep_id"] = "none"
            ctx["chain"] = None
        return render_manifest(jinja_env, template_name, ctx)

    @pytest.mark.parametrize("template", ["dag-pod.yaml.j2", "test-pod.yaml.j2"])
    def test_value_from_field_ref_rendered(self, env, template):
        env_vars = [
            {
                "name": "POD_IP",
                "valueFrom": {"fieldRef": {"fieldPath": "status.podIP"}},
            },
        ]
        manifest = self._render_pod(env, template, env_vars)
        doc = yaml.safe_load(manifest)
        pod_env = doc["spec"]["containers"][0]["env"]
        assert len(pod_env) == 1
        assert pod_env[0]["name"] == "POD_IP"
        assert "value" not in pod_env[0]
        assert pod_env[0]["valueFrom"]["fieldRef"]["fieldPath"] == "status.podIP"

    @pytest.mark.parametrize("template", ["dag-pod.yaml.j2", "test-pod.yaml.j2"])
    def test_value_from_secret_ref_rendered(self, env, template):
        env_vars = [
            {
                "name": "TOKEN",
                "valueFrom": {
                    "secretKeyRef": {"name": "my-secret", "key": "token"},
                },
            },
        ]
        manifest = self._render_pod(env, template, env_vars)
        doc = yaml.safe_load(manifest)
        pod_env = doc["spec"]["containers"][0]["env"]
        assert pod_env[0]["valueFrom"]["secretKeyRef"]["name"] == "my-secret"
        assert pod_env[0]["valueFrom"]["secretKeyRef"]["key"] == "token"

    @pytest.mark.parametrize("template", ["dag-pod.yaml.j2", "test-pod.yaml.j2"])
    def test_mixed_value_and_value_from(self, env, template):
        env_vars = [
            {"name": "HOME", "value": "/tmp"},
            {
                "name": "POD_IP",
                "valueFrom": {"fieldRef": {"fieldPath": "status.podIP"}},
            },
            {"name": "PORT", "value": "8000"},
        ]
        manifest = self._render_pod(env, template, env_vars)
        doc = yaml.safe_load(manifest)
        pod_env = doc["spec"]["containers"][0]["env"]
        assert len(pod_env) == 3
        assert pod_env[0] == {"name": "HOME", "value": "/tmp"}
        assert pod_env[1]["name"] == "POD_IP"
        assert "value" not in pod_env[1]
        assert pod_env[1]["valueFrom"]["fieldRef"]["fieldPath"] == "status.podIP"
        assert pod_env[2] == {"name": "PORT", "value": "8000"}

    @pytest.mark.parametrize("template", ["dag-pod.yaml.j2", "test-pod.yaml.j2"])
    def test_plain_value_still_works(self, env, template):
        env_vars = [{"name": "FOO", "value": "bar"}]
        manifest = self._render_pod(env, template, env_vars)
        doc = yaml.safe_load(manifest)
        pod_env = doc["spec"]["containers"][0]["env"]
        assert pod_env[0] == {"name": "FOO", "value": "bar"}


# -- custom pod labels --------------------------------------------------------


class TestCustomPodLabels:
    @pytest.fixture()
    def env(self):
        return create_jinja_env("templates")

    def _render_pod(self, jinja_env, template_name, extra_labels):
        ctx = {
            "pod_name": "test-pod",
            "namespace": "default",
            "managed_by_label": "uat",
            "test": "test",
            "dag_step_name": "step",
            "node": None,
            "privileged": False,
            "image": "img:latest",
            "command": None,
            "env": [],
            "ports": None,
            "readiness_probe": None,
            "resources": None,
            "volume_mounts": [],
            "volumes": [],
            "workspace_subpath": "ws",
            "binaries_subpath": "bin",
            "models_storage": None,
            "pvc": "pvc",
            "extra_labels": extra_labels,
        }
        if template_name == "test-pod.yaml.j2":
            ctx["sweep_id"] = "none"
            ctx["chain"] = None
        return render_manifest(jinja_env, template_name, ctx)

    @pytest.mark.parametrize("template", ["dag-pod.yaml.j2", "test-pod.yaml.j2"])
    def test_custom_labels_rendered(self, env, template):
        labels = {"llm-d.ai/role": "prefill", "app": "vllm"}
        manifest = self._render_pod(env, template, labels)
        doc = yaml.safe_load(manifest)
        pod_labels = doc["metadata"]["labels"]
        assert pod_labels["llm-d.ai/role"] == "prefill"
        assert pod_labels["app"] == "vllm"

    @pytest.mark.parametrize("template", ["dag-pod.yaml.j2", "test-pod.yaml.j2"])
    def test_empty_labels_no_effect(self, env, template):
        manifest = self._render_pod(env, template, {})
        doc = yaml.safe_load(manifest)
        pod_labels = doc["metadata"]["labels"]
        assert "llm-d.ai/role" not in pod_labels
        assert pod_labels["test"] == "test"
        assert pod_labels["dag-step"] == "step"

    @pytest.mark.parametrize("template", ["dag-pod.yaml.j2", "test-pod.yaml.j2"])
    def test_custom_labels_dont_overwrite_fixed(self, env, template):
        labels = {"test": "override-attempt", "custom": "ok"}
        manifest = self._render_pod(env, template, labels)
        doc = yaml.safe_load(manifest)
        pod_labels = doc["metadata"]["labels"]
        assert pod_labels["custom"] == "ok"
        # YAML last-key-wins: custom label overwrites the fixed one.
        # This is expected — test authors control their own labels.
        assert pod_labels["test"] == "override-attempt"


# -- sidecar containers -------------------------------------------------------


class TestSidecarModel:
    def test_sidecar_defaults(self):
        sc = SidecarContainer(name="proxy", image="img:latest")
        assert sc.command == []
        assert sc.args == []
        assert sc.env == []
        assert sc.ports == []
        assert sc.resources is None
        assert sc.volume_mounts == []

    def test_sidecar_with_all_fields(self):
        sc = SidecarContainer(
            name="proxy",
            image="img:latest",
            command=["./proxy"],
            args=["--port=8000"],
            env=[{"name": "FOO", "value": "bar"}],
            ports=[{"containerPort": 8000}],
            resources={"requests": {"cpu": "1"}},
            volumeMounts=[{"name": "shm", "mountPath": "/dev/shm"}],
        )
        assert sc.volume_mounts == [{"name": "shm", "mountPath": "/dev/shm"}]
        assert sc.args == ["--port=8000"]

    def test_dag_step_with_sidecars(self):
        step = DAGStep(
            name="decode",
            image="vllm:latest",
            sidecars=[
                {"name": "proxy", "image": "sidecar:latest", "args": ["--port=8000"]},
            ],
        )
        assert len(step.sidecars) == 1
        assert step.sidecars[0].name == "proxy"

    def test_resource_step_rejects_sidecars(self):
        with pytest.raises(ValueError, match="cannot have sidecars"):
            DAGStep(
                name="pool",
                sidecars=[{"name": "proxy", "image": "img"}],
                resourceConfig={
                    "apiVersion": "v1",
                    "kind": "ConfigMap",
                    "spec": {},
                },
            )


class TestSidecarTemplate:
    @pytest.fixture()
    def env(self):
        return create_jinja_env("templates")

    def _render_pod(self, jinja_env, template_name, sidecars):
        ctx = {
            "pod_name": "test-pod",
            "namespace": "default",
            "managed_by_label": "uat",
            "test": "test",
            "dag_step_name": "step",
            "node": None,
            "privileged": False,
            "image": "img:latest",
            "command": None,
            "env": [],
            "ports": None,
            "readiness_probe": None,
            "resources": None,
            "volume_mounts": [],
            "volumes": [],
            "workspace_subpath": "ws",
            "binaries_subpath": "bin",
            "models_storage": None,
            "pvc": "pvc",
            "extra_labels": {},
            "sidecars": sidecars,
        }
        if template_name == "test-pod.yaml.j2":
            ctx["sweep_id"] = "none"
            ctx["chain"] = None
        return render_manifest(jinja_env, template_name, ctx)

    @pytest.mark.parametrize("template", ["dag-pod.yaml.j2", "test-pod.yaml.j2"])
    def test_no_sidecars_no_init_containers(self, env, template):
        manifest = self._render_pod(env, template, [])
        doc = yaml.safe_load(manifest)
        assert "initContainers" not in doc["spec"]

    @pytest.mark.parametrize("template", ["dag-pod.yaml.j2", "test-pod.yaml.j2"])
    def test_sidecar_renders_as_init_container(self, env, template):
        sidecars = [
            {
                "name": "routing-proxy",
                "image": "ghcr.io/llm-d/sidecar:main",
                "args": ["--port=8000", "--vllm-port=8200"],
                "env": [],
                "ports": [
                    {"containerPort": 8000, "name": "sidecar", "protocol": "TCP"}
                ],
                "resources": {},
                "command": [],
                "volumeMounts": [],
            },
        ]
        manifest = self._render_pod(env, template, sidecars)
        doc = yaml.safe_load(manifest)
        init = doc["spec"]["initContainers"]
        assert len(init) == 1
        assert init[0]["name"] == "routing-proxy"
        assert init[0]["image"] == "ghcr.io/llm-d/sidecar:main"
        assert init[0]["restartPolicy"] == "Always"
        assert init[0]["args"] == ["--port=8000", "--vllm-port=8200"]
        assert init[0]["ports"][0]["containerPort"] == 8000

    @pytest.mark.parametrize("template", ["dag-pod.yaml.j2", "test-pod.yaml.j2"])
    def test_sidecar_env_with_value_from(self, env, template):
        sidecars = [
            {
                "name": "proxy",
                "image": "img",
                "env": [
                    {
                        "name": "POD_IP",
                        "valueFrom": {"fieldRef": {"fieldPath": "status.podIP"}},
                    },
                    {"name": "PORT", "value": "8000"},
                ],
                "args": [],
                "ports": [],
                "resources": {},
                "command": [],
                "volumeMounts": [],
            },
        ]
        manifest = self._render_pod(env, template, sidecars)
        doc = yaml.safe_load(manifest)
        sc_env = doc["spec"]["initContainers"][0]["env"]
        assert sc_env[0]["name"] == "POD_IP"
        assert "value" not in sc_env[0]
        assert sc_env[0]["valueFrom"]["fieldRef"]["fieldPath"] == "status.podIP"
        assert sc_env[1] == {"name": "PORT", "value": "8000"}

    @pytest.mark.parametrize("template", ["dag-pod.yaml.j2", "test-pod.yaml.j2"])
    def test_multiple_sidecars(self, env, template):
        sidecars = [
            {
                "name": "sc1",
                "image": "img1",
                "env": [],
                "args": [],
                "ports": [],
                "resources": {},
                "command": [],
                "volumeMounts": [],
            },
            {
                "name": "sc2",
                "image": "img2",
                "env": [],
                "args": [],
                "ports": [],
                "resources": {},
                "command": [],
                "volumeMounts": [],
            },
        ]
        manifest = self._render_pod(env, template, sidecars)
        doc = yaml.safe_load(manifest)
        init = doc["spec"]["initContainers"]
        assert len(init) == 2
        assert init[0]["name"] == "sc1"
        assert init[1]["name"] == "sc2"

    @pytest.mark.parametrize("template", ["dag-pod.yaml.j2", "test-pod.yaml.j2"])
    def test_sidecar_volume_mounts(self, env, template):
        sidecars = [
            {
                "name": "proxy",
                "image": "img",
                "env": [],
                "args": [],
                "ports": [],
                "resources": {},
                "command": [],
                "volumeMounts": [{"name": "shm", "mountPath": "/dev/shm"}],
            },
        ]
        manifest = self._render_pod(env, template, sidecars)
        doc = yaml.safe_load(manifest)
        vms = doc["spec"]["initContainers"][0]["volumeMounts"]
        assert vms[0]["name"] == "shm"
        assert vms[0]["mountPath"] == "/dev/shm"

    @pytest.mark.parametrize("template", ["dag-pod.yaml.j2", "test-pod.yaml.j2"])
    def test_main_container_unaffected_by_sidecar(self, env, template):
        sidecars = [
            {
                "name": "sc",
                "image": "sc-img",
                "env": [],
                "args": [],
                "ports": [],
                "resources": {},
                "command": [],
                "volumeMounts": [],
            },
        ]
        manifest = self._render_pod(env, template, sidecars)
        doc = yaml.safe_load(manifest)
        main = doc["spec"]["containers"][0]
        assert main["name"] == "test-pod"
        assert main["image"] == "img:latest"


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

    def test_dag_deep_merges_step_overrides(self):
        base = {
            "dag": [
                {
                    "name": "server",
                    "image": "img",
                    "service": {"enabled": True, "port": 8080},
                }
            ]
        }
        override = {"dag": {"server": {"service": {"port": 9090}}}}
        result = _deep_merge_spec(base, override)
        svc = result["dag"][0]["service"]
        assert svc["port"] == 9090
        assert svc["enabled"] is True

    def test_dag_unknown_step_raises(self):
        base = {"dag": [{"name": "server", "image": "img"}]}
        override = {"dag": {"typo": {"image": "new"}}}
        with pytest.raises(ValueError, match="unknown step 'typo'"):
            _deep_merge_spec(base, override)

    def test_dag_list_override_raises(self):
        base = {"dag": [{"name": "server", "image": "old"}]}
        override = {"dag": [{"name": "client", "image": "new"}]}
        with pytest.raises(TypeError, match="must be a dict keyed by step name"):
            _deep_merge_spec(base, override)


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

    def test_sidecar_jinja_rendering(self, env, tc):
        node_spec = {
            "componentValidation": {"sanity": {"nvidia.com/gpu": 8}},
        }
        dag_step = DAGStep(
            name="server",
            image="vllm:latest",
            sidecars=[
                SidecarContainer(
                    name="helper",
                    image="helper:latest",
                    command=["sh", "-c"],
                    args=[
                        "exec run --tp={{ nodeSpec.componentValidation.sanity['nvidia.com/gpu'] }}",
                    ],
                    resources={
                        "requests": {
                            "nvidia.com/gpu": '{% set g = nodeSpec.componentValidation.sanity["nvidia.com/gpu"] %}{{ g // 2 }}',
                        },
                        "limits": {
                            "nvidia.com/gpu": '{% set g = nodeSpec.componentValidation.sanity["nvidia.com/gpu"] %}{{ g // 2 }}',
                        },
                    },
                ),
            ],
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
            node_spec_dict=node_spec,
        )
        manifest = steps[0].content
        doc = yaml.safe_load(manifest)
        init = doc["spec"]["initContainers"]
        assert len(init) == 1
        sc = init[0]
        assert sc["args"] == ["exec run --tp=8"]
        assert sc["resources"]["requests"]["nvidia.com/gpu"] == "4"
        assert sc["resources"]["limits"]["nvidia.com/gpu"] == "4"


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
        sanity = {"nvidia.com/gpu": 4, **extra_sanity}
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


# -- ResourceConfig / DAGStep validation -------------------------------------


class TestResourceConfigValidation:
    def test_resource_step_no_image_required(self):
        step = DAGStep(
            name="pool",
            resourceConfig={
                "apiVersion": "v1",
                "kind": "ConfigMap",
                "spec": {"data": {"key": "val"}},
            },
        )
        assert step.image == ""
        assert step.resource_config.kind == "ConfigMap"

    def test_pod_step_requires_image(self):
        with pytest.raises(ValueError, match="requires an image"):
            DAGStep(name="server")

    def test_resource_step_rejects_persists_through_sweep(self):
        with pytest.raises(ValueError, match="cannot set persistsThroughSweep"):
            DAGStep(
                name="pool",
                persistsThroughSweep=True,
                resourceConfig={
                    "apiVersion": "v1",
                    "kind": "ConfigMap",
                    "spec": {},
                },
            )

    def test_resource_step_rejects_parameter_sweep(self):
        with pytest.raises(ValueError, match="cannot set parameterSweep"):
            DAGStep(
                name="pool",
                parameterSweep={
                    "baseCommand": {"args": ["cmd"]},
                    "entries": [{"id": "a"}],
                },
                resourceConfig={
                    "apiVersion": "v1",
                    "kind": "ConfigMap",
                    "spec": {},
                },
            )

    def test_resource_config_requires_fields(self):
        with pytest.raises(ValueError):
            ResourceConfig(kind="ConfigMap", spec={})


# -- _render_nested_strings ---------------------------------------------------


class TestRenderNestedStrings:
    @pytest.fixture()
    def env(self):
        return create_jinja_env("templates")

    def test_renders_string_values(self, env):
        ctx = {"name": "hello"}
        result = _render_nested_strings({"key": "{{ name }}"}, ctx, env)
        assert result == {"key": "hello"}

    def test_renders_nested_dicts(self, env):
        ctx = {"port": "8080"}
        data = {"outer": {"inner": "{{ port }}"}}
        result = _render_nested_strings(data, ctx, env)
        assert result == {"outer": {"inner": "8080"}}

    def test_renders_lists(self, env):
        ctx = {"x": "val"}
        data = {"items": ["{{ x }}", "static"]}
        result = _render_nested_strings(data, ctx, env)
        assert result == {"items": ["val", "static"]}

    def test_preserves_non_strings(self, env):
        data = {"count": 42, "flag": True, "empty": None}
        result = _render_nested_strings(data, {}, env)
        assert result == {"count": 42, "flag": True, "empty": None}

    def test_nested_list_of_dicts(self, env):
        ctx = {"p": "9000"}
        data = {"ports": [{"number": "{{ p }}"}]}
        result = _render_nested_strings(data, ctx, env)
        assert result == {"ports": [{"number": "9000"}]}


# -- resource.yaml.j2 template -----------------------------------------------


class TestResourceTemplate:
    @pytest.fixture()
    def env(self):
        return create_jinja_env("templates")

    def test_renders_basic_resource(self, env):
        ctx = {
            "api_version": "v1",
            "kind": "ConfigMap",
            "resource_name": "1-test-my-cm",
            "namespace": "ns",
            "managed_by_label": "uat",
            "test": "mytest",
            "node": "",
            "spec": {"data": {"key": "value"}},
        }
        content = render_manifest(env, "resource.yaml.j2", ctx)
        doc = yaml.safe_load(content)
        assert doc["apiVersion"] == "v1"
        assert doc["kind"] == "ConfigMap"
        assert doc["metadata"]["name"] == "1-test-my-cm"
        assert doc["metadata"]["namespace"] == "ns"
        assert doc["metadata"]["labels"]["test"] == "mytest"
        assert doc["metadata"]["labels"]["app.kubernetes.io/managed-by"] == "uat"
        assert "node" not in doc["metadata"]["labels"]
        assert doc["spec"]["data"]["key"] == "value"

    def test_includes_node_label(self, env):
        ctx = {
            "api_version": "v1",
            "kind": "ConfigMap",
            "resource_name": "res",
            "namespace": "ns",
            "managed_by_label": "uat",
            "test": "t",
            "node": "wrk-0",
            "spec": {},
        }
        content = render_manifest(env, "resource.yaml.j2", ctx)
        doc = yaml.safe_load(content)
        assert doc["metadata"]["labels"]["node"] == "wrk-0"

    def test_includes_chain_label(self, env):
        ctx = {
            "api_version": "v1",
            "kind": "ConfigMap",
            "resource_name": "res",
            "namespace": "ns",
            "managed_by_label": "uat",
            "test": "t",
            "node": "",
            "chain": "set0",
            "spec": {},
        }
        content = render_manifest(env, "resource.yaml.j2", ctx)
        doc = yaml.safe_load(content)
        assert doc["metadata"]["labels"]["chain"] == "set0"

    def test_crd_resource(self, env):
        ctx = {
            "api_version": "inference.networking.k8s.io/v1",
            "kind": "InferencePool",
            "resource_name": "1-llm-d-pool",
            "namespace": "ns",
            "managed_by_label": "uat",
            "test": "llm-d",
            "node": "wrk-6",
            "spec": {
                "selector": {"matchLabels": {"llm-d.ai/role": "prefill"}},
                "targetPortNumber": 8000,
            },
        }
        content = render_manifest(env, "resource.yaml.j2", ctx)
        doc = yaml.safe_load(content)
        assert doc["apiVersion"] == "inference.networking.k8s.io/v1"
        assert doc["kind"] == "InferencePool"
        assert doc["spec"]["selector"]["matchLabels"]["llm-d.ai/role"] == "prefill"
        assert doc["spec"]["targetPortNumber"] == 8000


# -- add_resource_steps -------------------------------------------------------


class TestAddResourceSteps:
    @pytest.fixture()
    def env(self):
        return create_jinja_env("templates")

    @pytest.fixture()
    def tc(self):
        return ToolConfig(**TC_DATA)

    def test_generates_two_steps(self, env, tc):
        dag_step = DAGStep(
            name="my-resource",
            resourceConfig={
                "apiVersion": "v1",
                "kind": "ConfigMap",
                "spec": {"data": {"key": "val"}},
            },
        )
        steps = []
        add_resource_steps(
            steps,
            dag_step,
            "1-t-wrk-0",
            "1-t-wrk-0",
            node="wrk-0",
            step_node="wrk-0",
            test=_loaded_test(),
            tc=tc,
            namespace="ns",
            services={},
            jinja_env=env,
            scope="node",
        )
        assert len(steps) == 2
        assert steps[0].type == "generate"
        assert steps[1].type == "command"
        assert steps[1].config["command"] == "apply"
        assert steps[1].config["probe"] == "none"

    def test_resource_name_follows_convention(self, env, tc):
        dag_step = DAGStep(
            name="pool",
            resourceConfig={
                "apiVersion": "v1",
                "kind": "ConfigMap",
                "spec": {},
            },
        )
        steps = []
        add_resource_steps(
            steps,
            dag_step,
            "1-t-wrk-0",
            "1-t-wrk-0",
            node="wrk-0",
            step_node="wrk-0",
            test=_loaded_test(),
            tc=tc,
            namespace="ns",
            services={},
            jinja_env=env,
            scope="node",
        )
        assert steps[0].resource_name == "1-t-wrk-0-pool"

    def test_manifest_has_harness_labels(self, env, tc):
        dag_step = DAGStep(
            name="pool",
            resourceConfig={
                "apiVersion": "v1",
                "kind": "ConfigMap",
                "spec": {"data": {}},
            },
        )
        steps = []
        add_resource_steps(
            steps,
            dag_step,
            "1-t-wrk-0",
            "1-t-wrk-0",
            node="wrk-0",
            step_node="wrk-0",
            test=_loaded_test(),
            tc=tc,
            namespace="ns",
            services={},
            jinja_env=env,
            scope="node",
        )
        doc = yaml.safe_load(steps[0].content)
        labels = doc["metadata"]["labels"]
        assert labels["app.kubernetes.io/managed-by"] == "uat"
        assert labels["test"] == "mytest"
        assert labels["node"] == "wrk-0"

    def test_jinja_rendering_in_spec(self, env, tc):
        dag_step = DAGStep(
            name="pool",
            resourceConfig={
                "apiVersion": "v1",
                "kind": "ConfigMap",
                "spec": {"data": {"model": "{{ serverConfig.model }}"}},
            },
        )
        test = _loaded_test()
        test.spec = TestSpec(
            source={"ginkgo": "t.go"},
            dag=[
                {"name": "run", "image": "img", "labelFilter": "pass-fail"},
            ],
            serverConfig={"model": "llama3"},
        )
        steps = []
        add_resource_steps(
            steps,
            dag_step,
            "1-t",
            "1-t",
            node="",
            step_node="",
            test=test,
            tc=tc,
            namespace="ns",
            services={},
            jinja_env=env,
            scope="project",
        )
        doc = yaml.safe_load(steps[0].content)
        assert doc["spec"]["data"]["model"] == "llama3"

    def test_services_available_in_rendering(self, env, tc):
        dag_step = DAGStep(
            name="pool",
            resourceConfig={
                "apiVersion": "v1",
                "kind": "ConfigMap",
                "spec": {"data": {"url": '{{ services["epp"].url }}'}},
            },
        )
        services = {
            "epp": {"url": "http://svc-epp:9002", "name": "svc-epp", "port": 9002}
        }
        steps = []
        add_resource_steps(
            steps,
            dag_step,
            "1-t",
            "1-t",
            node="",
            step_node="",
            test=_loaded_test(),
            tc=tc,
            namespace="ns",
            services=services,
            jinja_env=env,
            scope="project",
        )
        doc = yaml.safe_load(steps[0].content)
        assert doc["spec"]["data"]["url"] == "http://svc-epp:9002"


# -- add_teardown_steps with extra_resource_types -----------------------------


class TestTeardownResourceTypes:
    def test_default_resource_types(self):
        steps = []
        add_teardown_steps(
            steps,
            has_persistent=True,
            step_prefix="1-t",
            res_prefix="1-t",
            selector="test=t",
            step_node="",
            test=_loaded_test(),
            scope="project",
        )
        for s in steps:
            assert s.config["resource_types"] == "pods,services,deployments"

    def test_extra_resource_types_appended(self):
        steps = []
        add_teardown_steps(
            steps,
            has_persistent=True,
            step_prefix="1-t",
            res_prefix="1-t",
            selector="test=t",
            step_node="",
            test=_loaded_test(),
            scope="project",
            extra_resource_types={"InferencePool", "ConfigMap"},
        )
        for s in steps:
            assert (
                s.config["resource_types"]
                == "pods,services,deployments,ConfigMap,InferencePool"
            )
