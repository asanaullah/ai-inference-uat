# Assisted by Claude Opus 4.6
"""Pydantic schemas and dataclasses for the UAT test harness."""

from dataclasses import dataclass, field
from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, model_validator


# ---------------------------------------------------------------------------
# ToolConfig (config.yaml)
# ---------------------------------------------------------------------------


class ToolConfig(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    ose_cli_image: str = Field(alias="oseCLIImage")
    builder_image: str = Field(alias="builderImage")
    aggregator_image: str = Field(alias="aggregatorImage")
    configmap_name: str = Field(alias="configmapName")
    builder_pod_name: str = Field(alias="builderPodName")
    aggregator_pod_name: str = Field(alias="aggregatorPodName")
    node_selector_key: str = Field(alias="nodeSelectorKey")
    managed_by_label: str = Field(alias="managedByLabel")
    builder_timeout: str = Field("300s", alias="builderTimeout")
    aggregator_timeout: str = Field("120s", alias="aggregatorTimeout")
    deploy_timeout: str = Field("600s", alias="deployTimeout")
    default_test_timeout: str = Field("600s", alias="defaultTestTimeout")
    pipeline_timeout: str = Field("2h", alias="pipelineTimeout")
    finally_timeout: str = Field("15m", alias="finallyTimeout")
    ginkgo_version: str = Field("v2.32.0", alias="ginkgoVersion")


# ---------------------------------------------------------------------------
# TestSuite (test_suite.yaml)
# ---------------------------------------------------------------------------


class TestEntry(BaseModel):
    model_config = ConfigDict(populate_by_name=True)
    name: str
    scope: Literal["node", "cluster", "project"]
    on_failure: Literal["continue", "skipTest", "abort"] = Field(
        "continue", alias="onFailure"
    )
    timeout: Optional[str] = None


class TestSuiteSpec(BaseModel):
    tests: list[TestEntry]


class TestSuite(BaseModel):
    spec: TestSuiteSpec


# ---------------------------------------------------------------------------
# ClusterTest (cluster/*.yaml)
# ---------------------------------------------------------------------------


class SanityCheck(BaseModel):
    model_config = ConfigDict(extra="allow", populate_by_name=True)
    gpu_count: int = Field(alias="gpuCount")


class ComponentValidation(BaseModel):
    model_config = ConfigDict(extra="allow")
    sanity: SanityCheck


class NodeSpec(BaseModel):
    model_config = ConfigDict(extra="allow", populate_by_name=True)
    name: str
    sanitized_name: str = ""
    component_validation: ComponentValidation = Field(alias="componentValidation")


class StorageConfig(BaseModel):
    model_config = ConfigDict(populate_by_name=True)
    pvc: str
    base_path: str = Field(alias="basePath")


class ClusterTestSpec(BaseModel):
    nodes: list[NodeSpec]
    namespace: str
    storage: StorageConfig


class ClusterTest(BaseModel):
    spec: ClusterTestSpec


# ---------------------------------------------------------------------------
# Test (<test>.yaml)
# ---------------------------------------------------------------------------


class ServiceConfig(BaseModel):
    enabled: bool = False
    port: int = 8000
    name: str = ""
    headless: bool = True


class CommandConfig(BaseModel):
    args: list[str] = []
    flags: dict[str, Any] = {}


class SweepEntry(BaseModel):
    id: str
    description: str = ""
    flags: dict[str, Any] = {}


class ParameterSweep(BaseModel):
    model_config = ConfigDict(populate_by_name=True)
    base_command: CommandConfig = Field(alias="baseCommand")
    entries: list[SweepEntry]


class TestRequirements(BaseModel):
    gpu: bool = False


class DAGStep(BaseModel):
    model_config = ConfigDict(populate_by_name=True)
    name: str
    image: str
    command: Optional[CommandConfig] = None
    env: list[dict[str, Any]] = []
    service: ServiceConfig = Field(default_factory=ServiceConfig)
    ports: list[dict[str, Any]] = []
    readiness_probe: Optional[dict[str, Any]] = Field(None, alias="readinessProbe")
    resources: Optional[dict[str, Any]] = None
    volume_mounts: list[dict[str, Any]] = Field(default=[], alias="volumeMounts")
    volumes: list[dict[str, Any]] = []
    persists_through_sweep: bool = Field(False, alias="persistsThroughSweep")
    parameter_sweep: Optional[ParameterSweep] = Field(None, alias="parameterSweep")
    label_filter: Optional[str] = Field(None, alias="labelFilter")
    privileged: bool = False


class TestSource(BaseModel):
    model_config = ConfigDict(populate_by_name=True)
    ginkgo: str


class TestSpec(BaseModel):
    model_config = ConfigDict(populate_by_name=True)
    requirements: TestRequirements = Field(default_factory=TestRequirements)
    source: TestSource
    dag: list[DAGStep]
    server_config: dict[str, Any] = Field(default={}, alias="serverConfig")


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
    on_failure: str = "continue"
    timeout: Optional[str] = None
    test_id: str = ""
    scope: str = "node"


@dataclass
class Step:
    name: str
    type: str  # 'generate' or 'command'
    config: dict[str, Any]
    content: str = ""
    source: list[str] = field(default_factory=list)
    resource_name: str = ""
    node: str = ""
    test: str = ""
    test_id: str = ""
    on_failure: str = ""
    finally_step: bool = False
    scope: str = ""
    phase: str = ""


# ---------------------------------------------------------------------------
# StepsFile (steps.json)
# ---------------------------------------------------------------------------

_VALID_COMMANDS = {"apply", "exec", "delete", "delete-all"}
_VALID_PROBES = {"none", "wait-ready", "poll-completed"}


def _validate_section(steps: list[dict[str, Any]], section: str) -> None:
    gen_names: set[str] = set()
    pod_names: set[str] = set()
    for s in steps:
        name = s.get("name", "")
        stype = s.get("type", "")

        if not name:
            raise ValueError(f"Step in {section} missing 'name'")

        if stype not in ("generate", "command"):
            raise ValueError(f"Step '{name}' in {section} has invalid type '{stype}'")

        config = s.get("config", {})
        pod_name = config.get("pod_name")
        if pod_name:
            if pod_name in pod_names:
                raise ValueError(f"Duplicate pod name '{pod_name}' in {section}")
            pod_names.add(pod_name)

        if stype == "generate":
            gen_names.add(name)
            if not s.get("content"):
                raise ValueError(
                    f"Generate step '{name}' in {section} has empty content"
                )
            if "output" not in config:
                raise ValueError(
                    f"Generate step '{name}' in {section} missing config.output"
                )
        else:
            cmd = config.get("command")
            if cmd not in _VALID_COMMANDS:
                raise ValueError(
                    f"Command step '{name}' in {section} has invalid "
                    f"command '{cmd}' (expected one of {_VALID_COMMANDS})"
                )
            probe = config.get("probe", "none")
            if probe not in _VALID_PROBES:
                raise ValueError(
                    f"Command step '{name}' in {section} has invalid "
                    f"probe '{probe}' (expected one of {_VALID_PROBES})"
                )
            for src in s.get("source", []):
                if src not in gen_names:
                    raise ValueError(
                        f"Command step '{name}' in {section} references "
                        f"source '{src}' which is not a preceding generate step"
                    )


_VALID_ON_FAILURE = {"continue", "skipTest", "abort"}


def _validate_on_failure(steps: list[dict[str, Any]]) -> None:
    for s in steps:
        if s.get("type") != "command":
            continue
        test = s.get("test", "")
        on_failure = s.get("on_failure", "")
        if test:
            if on_failure not in _VALID_ON_FAILURE:
                raise ValueError(
                    f"Step '{s.get('name')}' has test='{test}' but "
                    f"on_failure='{on_failure}' "
                    f"(expected one of {_VALID_ON_FAILURE})"
                )
        else:
            if on_failure:
                raise ValueError(
                    f"Step '{s.get('name')}' has no test but "
                    f"on_failure='{on_failure}' (expected empty)"
                )


class StepsFile(BaseModel):
    metadata: dict[str, Any]
    steps: list[dict[str, Any]]

    @model_validator(mode="after")
    def validate_structure(self) -> "StepsFile":
        for key in ("toolConfig", "clusterSpec"):
            if key not in self.metadata:
                raise ValueError(f"metadata missing required key '{key}'")
        ToolConfig(**self.metadata["toolConfig"])
        ClusterTestSpec(**self.metadata["clusterSpec"])

        _validate_section(self.steps, "steps")
        _validate_on_failure(self.steps)
        return self
