# Assisted by Claude Opus 4.6
"""Node-level step computation, DAG/test pod rendering, and requirement checks."""

from typing import Any

from jinja2 import Environment

from .common import (
    add_ephemeral_steps,
    add_persistent_steps,
    add_resource_steps,
    add_teardown_steps,
    fit,
)
from .models import LoadedTest, ModelsStorageConfig, NodeSpec, Step, ToolConfig


def compute_node_steps(
    node_spec: NodeSpec,
    test: LoadedTest,
    tool_config: ToolConfig,
    namespace: str,
    pvc: str,
    base_path: str,
    jinja_env: Environment,
    models_storage: ModelsStorageConfig | None = None,
    peer_namespace: str = "",
    peer_pvc: str = "",
    peer_base_path: str = "",
    peer_models_storage: ModelsStorageConfig | None = None,
) -> list[Step]:
    steps: list[Step] = []
    node = node_spec.name
    node_spec_dict = node_spec.model_dump(by_alias=True)

    services: dict[str, dict[str, Any]] = {}
    has_persistent = False
    has_peer_persistent = False
    has_peer = False
    extra_resource_types: set[str] = set()
    extra_peer_resource_types: set[str] = set()

    step_prefix = f"{test.test_id}-{test.name}-{node}"

    for dag_step in test.spec.dag:
        step_ns = peer_namespace if dag_step.peer else namespace
        step_pvc = (peer_pvc or pvc) if dag_step.peer else pvc
        step_base_path = (peer_base_path or base_path) if dag_step.peer else base_path
        step_models = peer_models_storage if dag_step.peer else models_storage
        if dag_step.peer:
            has_peer = True
        if dag_step.resource_config:
            if dag_step.peer:
                has_peer_persistent = True
                extra_peer_resource_types.add(dag_step.resource_config.kind)
            else:
                has_persistent = True
                extra_resource_types.add(dag_step.resource_config.kind)
            add_resource_steps(
                steps,
                dag_step,
                step_prefix,
                node=node,
                step_node=node,
                test=test,
                tc=tool_config,
                namespace=step_ns,
                services=services,
                jinja_env=jinja_env,
                scope="node",
                node_spec_dict=node_spec_dict,
            )
        elif dag_step.persists_through_sweep:
            if dag_step.peer:
                has_peer_persistent = True
            else:
                has_persistent = True
            add_persistent_steps(
                steps,
                dag_step,
                step_prefix,
                node=node,
                step_node=node,
                test=test,
                tc=tool_config,
                namespace=step_ns,
                pvc=step_pvc,
                base_path=step_base_path,
                services=services,
                jinja_env=jinja_env,
                scope="node",
                node_spec_dict=node_spec_dict,
                models_storage=step_models,
            )
        else:
            node_label = fit(node, 10)
            add_ephemeral_steps(
                steps,
                dag_step,
                step_prefix,
                node=node,
                step_node=node,
                test=test,
                tc=tool_config,
                namespace=step_ns,
                pvc=step_pvc,
                base_path=step_base_path,
                services=services,
                jinja_env=jinja_env,
                scope="node",
                selector_extra=f",node={node_label}",
                node_spec_dict=node_spec_dict,
                models_storage=step_models,
            )

    test_label = fit(test.name, 63)
    node_label = fit(node, 10)
    selector = f"test={test_label},node={node_label}"
    add_teardown_steps(
        steps,
        has_persistent,
        step_prefix,
        selector=selector,
        step_node=node,
        test=test,
        scope="node",
        node=node,
        extra_resource_types=extra_resource_types,
        namespace=namespace,
    )
    if has_peer and peer_namespace:
        add_teardown_steps(
            steps,
            has_peer_persistent,
            f"{step_prefix}-peer",
            selector=selector,
            step_node=node,
            test=test,
            scope="node",
            node=node,
            extra_resource_types=extra_peer_resource_types,
            namespace=peer_namespace,
        )

    return steps
