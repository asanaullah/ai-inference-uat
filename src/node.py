# Assisted by Claude Opus 4.6
"""Node-level step computation, DAG/test pod rendering, and requirement checks."""

from typing import Any

from jinja2 import Environment

from .common import add_ephemeral_steps, add_persistent_steps, add_teardown_steps
from .models import LoadedTest, NodeSpec, Step, ToolConfig


def node_meets_requirements(requirements: Any, node_spec: NodeSpec) -> bool:
    if requirements.gpu and node_spec.component_validation.sanity.gpu_count <= 0:
        return False
    return True


def compute_node_steps(
    node_spec: NodeSpec,
    test: LoadedTest,
    tool_config: ToolConfig,
    namespace: str,
    pvc: str,
    base_path: str,
    jinja_env: Environment,
) -> list[Step]:
    steps: list[Step] = []
    node = node_spec.name
    safe_node = node_spec.sanitized_name or node
    node_spec_dict = node_spec.model_dump(by_alias=True)

    if not node_meets_requirements(test.spec.requirements, node_spec):
        print(f"  Skipping {test.name} on {node} (requirements not met)")
        return steps

    services: dict[str, dict[str, Any]] = {}
    has_persistent = False

    step_prefix = f"{test.test_id}-{test.name}-{node}"
    res_prefix = f"{test.test_id}-{test.name}-{safe_node}"

    for dag_step in test.spec.dag:
        if dag_step.persists_through_sweep:
            has_persistent = True
            add_persistent_steps(
                steps,
                dag_step,
                step_prefix,
                res_prefix,
                node=node,
                step_node=node,
                test=test,
                tc=tool_config,
                namespace=namespace,
                pvc=pvc,
                base_path=base_path,
                services=services,
                jinja_env=jinja_env,
                scope="node",
                node_spec_dict=node_spec_dict,
            )
        else:
            add_ephemeral_steps(
                steps,
                dag_step,
                step_prefix,
                res_prefix,
                node=node,
                step_node=node,
                test=test,
                tc=tool_config,
                namespace=namespace,
                pvc=pvc,
                base_path=base_path,
                services=services,
                jinja_env=jinja_env,
                scope="node",
                selector_extra=f",node={node}",
                node_spec_dict=node_spec_dict,
            )

    add_teardown_steps(
        steps,
        has_persistent,
        step_prefix,
        res_prefix,
        selector=f"test={test.name},node={node}",
        step_node=node,
        test=test,
        scope="node",
    )

    return steps
