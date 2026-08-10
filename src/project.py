# Assisted by Claude Opus 4.6
"""Project-level step computation (single chain, no node affinity)."""

from typing import Any

from jinja2 import Environment

from .common import (
    add_ephemeral_steps,
    add_persistent_steps,
    add_resource_steps,
    add_teardown_steps,
)
from .models import LoadedTest, ModelsStorageConfig, Step, ToolConfig


def compute_project_steps(
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
    step_prefix = f"{test.test_id}-{test.name}"
    res_prefix = step_prefix
    scope = "project"

    services: dict[str, dict[str, Any]] = {}
    has_persistent = False
    has_peer_persistent = False
    has_peer = False
    extra_resource_types: set[str] = set()
    extra_peer_resource_types: set[str] = set()

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
                res_prefix,
                node="",
                step_node="",
                test=test,
                tc=tool_config,
                namespace=step_ns,
                services=services,
                jinja_env=jinja_env,
                scope=scope,
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
                res_prefix,
                node="",
                step_node="",
                test=test,
                tc=tool_config,
                namespace=step_ns,
                pvc=step_pvc,
                base_path=step_base_path,
                services=services,
                jinja_env=jinja_env,
                scope=scope,
                models_storage=step_models,
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
                namespace=step_ns,
                pvc=step_pvc,
                base_path=step_base_path,
                services=services,
                jinja_env=jinja_env,
                scope=scope,
                models_storage=step_models,
            )

    selector = f"test={test.name}"
    add_teardown_steps(
        steps,
        has_persistent,
        step_prefix,
        res_prefix,
        selector=selector,
        step_node="",
        test=test,
        scope=scope,
        extra_resource_types=extra_resource_types,
        namespace=namespace,
    )
    if has_peer and peer_namespace:
        add_teardown_steps(
            steps,
            has_peer_persistent,
            f"{step_prefix}-peer",
            f"{res_prefix}-peer",
            selector=selector,
            step_node="",
            test=test,
            scope=scope,
            extra_resource_types=extra_peer_resource_types,
            namespace=peer_namespace,
        )

    return steps
