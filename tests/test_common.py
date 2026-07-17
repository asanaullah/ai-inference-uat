import pytest

from src.common import (
    build_command,
    create_jinja_env,
    sanitize_node_name,
    validate_manifest,
    _yaml_quote,
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
        with pytest.raises(ValueError, match="not a mapping"):
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
