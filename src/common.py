# Assisted by Claude Opus 4.6
"""Jinja2 engine, manifest validation, config loading, and shared utilities."""

import hashlib
import json
import re
import shlex
from pathlib import Path
from typing import Any

import yaml
from jinja2 import Environment, FileSystemLoader, StrictUndefined

from .models import ClusterTest, LoadedTest, Test, TestSuite, ToolConfig


def create_jinja_env(template_dir: str | Path) -> Environment:
    env = Environment(
        loader=FileSystemLoader(str(template_dir)),
        undefined=StrictUndefined,
        trim_blocks=True,
        lstrip_blocks=True,
        keep_trailing_newline=True,
    )
    env.filters["to_yaml"] = _to_yaml
    env.filters["toJson"] = _to_json
    env.filters["yaml_quote"] = _yaml_quote
    env.filters["shell_join"] = _shell_join
    return env


def _to_yaml(value: Any) -> str:
    return yaml.dump(value, default_flow_style=False).rstrip("\n")


def _to_json(value: Any) -> str:
    return json.dumps(value)


_YAML11_BOOLEANS = frozenset(
    {
        "true",
        "false",
        "yes",
        "no",
        "on",
        "off",
        "True",
        "False",
        "Yes",
        "No",
        "On",
        "Off",
        "TRUE",
        "FALSE",
        "YES",
        "NO",
        "ON",
        "OFF",
    }
)


def _yaml_quote(value: str) -> str:
    s = str(value)
    if not s or any(c in s for c in ":{}[],\"'|>&*#?!%@") or s != s.strip():
        return json.dumps(s)
    if s in _YAML11_BOOLEANS or s in ("null", "Null", "NULL", "~"):
        return json.dumps(s)
    try:
        float(s)
        return json.dumps(s)
    except ValueError:
        pass
    return s


def _shell_join(value: list[str]) -> str:
    return shlex.join(value)


def render_template(
    env: Environment, template_name: str, context: dict[str, Any]
) -> str:
    template = env.get_template(template_name)
    return template.render(context)


def render_manifest(
    env: Environment, template_name: str, context: dict[str, Any]
) -> str:
    content = render_template(env, template_name, context)
    validate_manifest(content)
    return content


def render_string(
    env: Environment, template_string: str, context: dict[str, Any]
) -> str:
    template = env.from_string(template_string)
    return template.render(context)


# Checks structural minimums only; additional validation (e.g. schema
# validation, dry-run) can be added in the future.
def validate_manifest(content: str) -> None:
    for doc in yaml.safe_load_all(content):
        if doc is None:
            continue
        if not isinstance(doc, dict):
            raise ValueError(f"Manifest document is not a mapping: {doc}")
        for required in ("apiVersion", "kind"):
            if required not in doc:
                raise ValueError(
                    f"Manifest missing required field '{required}': "
                    f"{doc.get('metadata', {}).get('name', '<unknown>')}"
                )
        metadata = doc.get("metadata", {})
        if "name" not in metadata and "generateName" not in metadata:
            raise ValueError(
                f"Manifest missing metadata.name or metadata.generateName: "
                f"{doc.get('kind', '<unknown>')}"
            )


def load_tool_config(config_path: str | Path) -> ToolConfig:
    with open(config_path) as f:
        data = yaml.safe_load(f)
    return ToolConfig(**data)


def load_config(
    suite_dir: str | Path,
    cluster_path: str | Path,
) -> tuple[TestSuite, ClusterTest, list[LoadedTest]]:
    suite_dir = Path(suite_dir)

    with open(suite_dir / "test_suite.yaml") as f:
        suite = TestSuite(**yaml.safe_load(f))

    with open(cluster_path) as f:
        cluster = ClusterTest(**yaml.safe_load(f))

    tests: list[LoadedTest] = []
    for i, entry in enumerate(suite.spec.tests, 1):
        test_id = str(i)
        with open(suite_dir / f"{entry.name}.yaml") as f:
            test_def = Test(**yaml.safe_load(f))

        go_source = (suite_dir / test_def.spec.source.ginkgo).read_text()

        tests.append(
            LoadedTest(
                name=entry.name,
                spec=test_def.spec,
                go_source=go_source,
                on_failure=entry.on_failure,
                timeout=entry.timeout,
                test_id=test_id,
                scope=entry.scope,
            )
        )

    return suite, cluster, tests


def build_command(args: list[str], flags: dict[str, Any]) -> list[str]:
    cmd = list(args)
    for key, value in flags.items():
        cmd.append(f"--{key}={value}")
    return cmd


_INVALID_RFC1123 = re.compile(r"[^a-z0-9\-]")


def sanitize_node_name(name: str) -> str:
    sanitized = _INVALID_RFC1123.sub("-", name.lower()).strip("-")
    if len(sanitized) <= 16:
        return sanitized
    h = hashlib.sha256(name.encode()).hexdigest()[:4]
    return f"{sanitized[:12]}-{h}"
