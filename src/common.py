# Assisted by Claude Opus 4.6
"""Jinja2 engine, manifest validation, config loading, and shared utilities."""

from __future__ import annotations

import json
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
    env.filters['to_yaml'] = _to_yaml
    env.filters['toJson'] = _to_json
    env.filters['yaml_quote'] = _yaml_quote
    env.filters['shell_join'] = _shell_join
    return env


def _to_yaml(value: Any) -> str:
    return yaml.dump(value, default_flow_style=False).rstrip('\n')


def _to_json(value: Any) -> str:
    return json.dumps(value)


def _yaml_quote(value: str) -> str:
    s = str(value)
    if not s or any(c in s for c in ':{}[],"\'|>&*#?!%@') or s != s.strip():
        return json.dumps(s)
    return s


def _shell_join(value: list[str]) -> str:
    return shlex.join(value)


def render_template(env: Environment, template_name: str, context: dict[str, Any]) -> str:
    template = env.get_template(template_name)
    return template.render(context)


def render_manifest(env: Environment, template_name: str, context: dict[str, Any]) -> str:
    content = render_template(env, template_name, context)
    validate_manifest(content)
    return content


def render_string(env: Environment, template_string: str, context: dict[str, Any]) -> str:
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
        for required in ('apiVersion', 'kind'):
            if required not in doc:
                raise ValueError(
                    f"Manifest missing required field '{required}': "
                    f"{doc.get('metadata', {}).get('name', '<unknown>')}"
                )
        metadata = doc.get('metadata', {})
        if 'name' not in metadata and 'generateName' not in metadata:
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

    with open(suite_dir / 'test_suite.yaml') as f:
        suite = TestSuite(**yaml.safe_load(f))

    with open(cluster_path) as f:
        cluster = ClusterTest(**yaml.safe_load(f))

    tests: list[LoadedTest] = []
    for test_name in suite.spec.tests.node:
        with open(suite_dir / f'{test_name}.yaml') as f:
            test_def = Test(**yaml.safe_load(f))

        go_source = (suite_dir / test_def.spec.source.ginkgo).read_text()
        go_mod = (suite_dir / test_def.spec.source.go_mod).read_text()
        go_sum = (suite_dir / test_def.spec.source.go_sum).read_text()

        tests.append(LoadedTest(
            name=test_name,
            spec=test_def.spec,
            go_source=go_source,
            go_mod=go_mod,
            go_sum=go_sum,
        ))

    return suite, cluster, tests


def build_command(args: list[str], flags: dict[str, Any]) -> list[str]:
    cmd = list(args)
    for key, value in flags.items():
        cmd.append(f'--{key}={value}')
    return cmd
