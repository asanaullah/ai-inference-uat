# Assisted by Claude Opus 4.6
"""Project-level step computation (single chain, no node affinity)."""

from typing import Any

from jinja2 import Environment

from .common import add_ephemeral_steps, add_persistent_steps, add_teardown_steps
from .models import LoadedTest, Step, ToolConfig


def compute_project_steps(
    test: LoadedTest,
    tool_config: ToolConfig,
    namespace: str,
    pvc: str,
    base_path: str,
    jinja_env: Environment,
) -> list[Step]:
    steps: list[Step] = []
    step_prefix = f"{test.test_id}-{test.name}"
    res_prefix = step_prefix
    scope = "project"

    services: dict[str, dict[str, Any]] = {}
    has_persistent = False

    for dag_step in test.spec.dag:
        if dag_step.persists_through_sweep:
            has_persistent = True
            add_persistent_steps(
                steps,
                dag_step,
                step_prefix,
                res_prefix,
                node="",
                step_node="",
                test=test,
                tc=tool_config,
                namespace=namespace,
                pvc=pvc,
                base_path=base_path,
                services=services,
                jinja_env=jinja_env,
                scope=scope,
            )
        else:
            add_ephemeral_steps(
                steps,
                dag_step,
                step_prefix,
                res_prefix,
                node="",
                step_node="",
                test=test,
                tc=tool_config,
                namespace=namespace,
                pvc=pvc,
                base_path=base_path,
                services=services,
                jinja_env=jinja_env,
                scope=scope,
            )

    add_teardown_steps(
        steps,
        has_persistent,
        step_prefix,
        res_prefix,
        selector=f"test={test.name}",
        step_node="",
        test=test,
        scope=scope,
    )

    return steps
