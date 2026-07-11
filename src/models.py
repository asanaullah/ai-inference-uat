# Assisted by Claude Opus 4.6
"""Pydantic schemas and dataclasses for the UAT test harness."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, Field, model_validator


# ---------------------------------------------------------------------------
# ToolConfig (config.yaml)
# ---------------------------------------------------------------------------

class ToolConfig(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    ose_cli_image: str = Field(alias='oseCLIImage')
    builder_image: str = Field(alias='builderImage')
    aggregator_image: str = Field(alias='aggregatorImage')
    configmap_name: str = Field(alias='configmapName')
    builder_pod_name: str = Field(alias='builderPodName')
    aggregator_pod_name: str = Field(alias='aggregatorPodName')
    node_selector_key: str = Field(alias='nodeSelectorKey')
    managed_by_label: str = Field(alias='managedByLabel')
    builder_timeout: str = Field('300s', alias='builderTimeout')
    aggregator_timeout: str = Field('120s', alias='aggregatorTimeout')
    deploy_timeout: str = Field('600s', alias='deployTimeout')
    test_timeout: str = Field('600s', alias='testTimeout')


# ---------------------------------------------------------------------------
# TestSuite (test_suite.yaml)
# ---------------------------------------------------------------------------

class ExecutionConfig(BaseModel):
    model_config = ConfigDict(populate_by_name=True)
    stop_on_failure: bool = Field(False, alias='stopOnFailure')


class TestCategories(BaseModel):
    node: list[str] = []
    cluster: list[str] = []
    project: list[str] = []


class TestSuiteSpec(BaseModel):
    tests: TestCategories
    execution: ExecutionConfig = Field(default_factory=ExecutionConfig)


class TestSuite(BaseModel):
    spec: TestSuiteSpec


# ---------------------------------------------------------------------------
# ClusterTest (cluster/*.yaml)
# ---------------------------------------------------------------------------

class SanityCheck(BaseModel):
    model_config = ConfigDict(extra='allow', populate_by_name=True)
    gpu_count: int = Field(alias='gpuCount')


class ComponentValidation(BaseModel):
    model_config = ConfigDict(extra='allow')
    sanity: SanityCheck


class NodeSpec(BaseModel):
    model_config = ConfigDict(extra='allow', populate_by_name=True)
    name: str
    component_validation: ComponentValidation = Field(alias='componentValidation')


class StorageConfig(BaseModel):
    model_config = ConfigDict(populate_by_name=True)
    pvc: str
    base_path: str = Field(alias='basePath')


class ClusterTestSpec(BaseModel):
    nodes: list[NodeSpec]
    namespace: str
    storage: StorageConfig
    timeout: str = '2h'


class ClusterTest(BaseModel):
    spec: ClusterTestSpec


# ---------------------------------------------------------------------------
# Test (<test>.yaml)
# ---------------------------------------------------------------------------

class ServiceConfig(BaseModel):
    enabled: bool = False
    port: int = 8000
    name: str = ''


class CommandConfig(BaseModel):
    args: list[str] = []
    flags: dict[str, Any] = {}


class SweepEntry(BaseModel):
    id: str
    description: str = ''
    flags: dict[str, Any] = {}


class ParameterSweep(BaseModel):
    model_config = ConfigDict(populate_by_name=True)
    base_command: CommandConfig = Field(alias='baseCommand')
    entries: list[SweepEntry]


class TestRequirements(BaseModel):
    gpu: bool = False


class DAGStep(BaseModel):
    model_config = ConfigDict(populate_by_name=True)
    name: str
    type: str = 'pod'
    image: str
    command: Optional[CommandConfig] = None
    env: list[dict[str, Any]] = []
    service: ServiceConfig = Field(default_factory=ServiceConfig)
    ports: list[dict[str, Any]] = []
    readiness_probe: Optional[dict[str, Any]] = Field(None, alias='readinessProbe')
    resources: Optional[dict[str, Any]] = None
    volume_mounts: list[dict[str, Any]] = Field(default=[], alias='volumeMounts')
    volumes: list[dict[str, Any]] = []
    persists_through_sweep: bool = Field(False, alias='persistsThroughSweep')
    parameter_sweep: Optional[ParameterSweep] = Field(None, alias='parameterSweep')
    label_filter: Optional[str] = Field(None, alias='labelFilter')
    privileged: bool = False
    depends_on: list[str] = Field(default=[], alias='dependsOn')


class TestSource(BaseModel):
    model_config = ConfigDict(populate_by_name=True)
    ginkgo: str
    go_mod: str = Field(alias='goMod')
    go_sum: str = Field(alias='goSum')


class TestSpec(BaseModel):
    model_config = ConfigDict(populate_by_name=True)
    requirements: TestRequirements = Field(default_factory=TestRequirements)
    source: TestSource
    dag: list[DAGStep]
    server_config: dict[str, Any] = Field(default={}, alias='serverConfig')


class Test(BaseModel):
    spec: TestSpec


# ---------------------------------------------------------------------------
# Dataclasses (runtime objects, no YAML source)
# ---------------------------------------------------------------------------

@dataclass
class LoadedTest:
    name: str
    spec: TestSpec
    go_source: str
    go_mod: str
    go_sum: str


@dataclass
class Step:
    name: str
    type: str  # 'generate' or 'command'
    config: dict[str, Any]
    content: str = ''
    source: list[str] = field(default_factory=list)
    node: str = ''
    test: str = ''


# ---------------------------------------------------------------------------
# StepsFile (steps.json)
# ---------------------------------------------------------------------------

_VALID_COMMANDS = {'apply', 'exec', 'delete', 'delete-all'}
_VALID_PROBES = {'none', 'wait-ready', 'poll-completed'}


def _validate_section(steps: list[dict[str, Any]], section: str) -> None:
    names: set[str] = set()
    gen_names: set[str] = set()
    for s in steps:
        name = s.get('name', '')
        stype = s.get('type', '')

        if not name:
            raise ValueError(f"Step in {section} missing 'name'")
        if name in names:
            raise ValueError(f"Duplicate step name '{name}' in {section}")
        names.add(name)

        if stype not in ('generate', 'command'):
            raise ValueError(
                f"Step '{name}' in {section} has invalid type '{stype}'")

        config = s.get('config', {})
        if stype == 'generate':
            gen_names.add(name)
            if not s.get('content'):
                raise ValueError(
                    f"Generate step '{name}' in {section} has empty content")
            if 'output' not in config:
                raise ValueError(
                    f"Generate step '{name}' in {section} missing config.output")
        else:
            cmd = config.get('command')
            if cmd not in _VALID_COMMANDS:
                raise ValueError(
                    f"Command step '{name}' in {section} has invalid "
                    f"command '{cmd}' (expected one of {_VALID_COMMANDS})")
            probe = config.get('probe', 'none')
            if probe not in _VALID_PROBES:
                raise ValueError(
                    f"Command step '{name}' in {section} has invalid "
                    f"probe '{probe}' (expected one of {_VALID_PROBES})")
            for src in s.get('source', []):
                if src not in gen_names:
                    raise ValueError(
                        f"Command step '{name}' in {section} references "
                        f"source '{src}' which is not a preceding generate step")


class StepsFile(BaseModel):
    metadata: dict[str, Any]
    setup: list[dict[str, Any]]
    nodes: dict[str, list[dict[str, Any]]]
    teardown: list[dict[str, Any]]

    @model_validator(mode='after')
    def validate_structure(self) -> 'StepsFile':
        for key in ('toolConfig', 'clusterSpec', 'stopOnFailure'):
            if key not in self.metadata:
                raise ValueError(f"metadata missing required key '{key}'")
        ToolConfig(**self.metadata['toolConfig'])
        ClusterTestSpec(**self.metadata['clusterSpec'])

        _validate_section(self.setup, 'setup')
        for node, steps in self.nodes.items():
            _validate_section(steps, f'nodes.{node}')
        _validate_section(self.teardown, 'teardown')
        return self
