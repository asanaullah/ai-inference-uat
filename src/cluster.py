# Assisted by Claude Opus 4.6
"""Cluster-level step computation (placeholder)."""

from jinja2 import Environment

from .models import LoadedTest, NodeSpec, Step, ToolConfig


def compute_cluster_steps(
    test: LoadedTest,
    tool_config: ToolConfig,
    namespace: str,
    pvc: str,
    base_path: str,
    jinja_env: Environment,
    nodes: list[NodeSpec] | None = None,
) -> list[Step]:
    return []
