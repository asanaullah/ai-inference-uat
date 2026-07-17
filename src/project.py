# Assisted by Claude Opus 4.6
"""Project-level step computation (placeholder)."""

from jinja2 import Environment

from .models import LoadedTest, Step, ToolConfig


def compute_project_steps(
    test: LoadedTest,
    tool_config: ToolConfig,
    namespace: str,
    pvc: str,
    base_path: str,
    jinja_env: Environment,
) -> list[Step]:
    return []
