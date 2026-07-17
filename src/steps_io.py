# Assisted by Claude Opus 4.6
"""Read and write the steps.json intermediate DAG representation."""

import json
from dataclasses import asdict
from pathlib import Path

from .models import ClusterTestSpec, Step, StepsFile, ToolConfig


def write_steps_file(
    steps: list[Step],
    tc: ToolConfig,
    cs: ClusterTestSpec,
    path: Path,
) -> None:
    data = {
        "metadata": {
            "toolConfig": tc.model_dump(by_alias=True),
            "clusterSpec": cs.model_dump(by_alias=True),
        },
        "steps": [asdict(s) for s in steps],
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2))


def load_steps_file(
    path: Path,
) -> tuple[list[Step], ToolConfig, ClusterTestSpec]:
    with open(path) as f:
        data = json.load(f)

    sf = StepsFile(**data)

    tc = ToolConfig(**sf.metadata["toolConfig"])
    cs = ClusterTestSpec(**sf.metadata["clusterSpec"])

    steps = [Step(**s) for s in sf.steps]

    return steps, tc, cs
