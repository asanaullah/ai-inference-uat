# Assisted by Claude Opus 4.6
"""Read and write the steps.json intermediate DAG representation."""

from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path

from .models import ClusterTestSpec, Step, StepsFile, ToolConfig


def write_steps(
    setup_steps: list[Step],
    node_steps: dict[str, list[Step]],
    teardown_steps: list[Step],
    tc: ToolConfig,
    cs: ClusterTestSpec,
    stop_on_failure: bool,
    path: Path,
) -> None:
    data = {
        "metadata": {
            "toolConfig": tc.model_dump(by_alias=True),
            "clusterSpec": cs.model_dump(by_alias=True),
            "stopOnFailure": stop_on_failure,
        },
        "setup": [asdict(s) for s in setup_steps],
        "nodes": {n: [asdict(s) for s in sl] for n, sl in node_steps.items()},
        "teardown": [asdict(s) for s in teardown_steps],
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2))


def load_steps(
    path: Path,
) -> tuple[
    list[Step], dict[str, list[Step]], list[Step], ToolConfig, ClusterTestSpec, bool
]:
    with open(path) as f:
        data = json.load(f)

    sf = StepsFile(**data)

    tc = ToolConfig(**sf.metadata["toolConfig"])
    cs = ClusterTestSpec(**sf.metadata["clusterSpec"])
    stop_on_failure = sf.metadata["stopOnFailure"]

    setup = [Step(**s) for s in sf.setup]
    nodes = {n: [Step(**s) for s in sl] for n, sl in sf.nodes.items()}
    teardown = [Step(**s) for s in sf.teardown]

    return setup, nodes, teardown, tc, cs, stop_on_failure
