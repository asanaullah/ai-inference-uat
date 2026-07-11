import pytest

from src.common import build_command, create_jinja_env, validate_manifest, _yaml_quote


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
