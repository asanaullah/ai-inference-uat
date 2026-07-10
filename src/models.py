# Assisted by Claude Opus 4.6
"""Pydantic schemas and dataclasses for the UAT test harness."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, Field


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
