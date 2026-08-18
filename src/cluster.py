# Assisted by Claude Opus 4.6
"""Cluster-level step computation, placement resolution, and resource validation."""

import itertools
import random
from typing import Any

from jinja2 import Environment

from .common import (
    add_ephemeral_steps,
    add_persistent_steps,
    add_resource_steps,
    add_teardown_steps,
    fit,
    render_string,
    validate_node_resources,
)
from .models import (
    DAGStep,
    LoadedTest,
    ModelsStorageConfig,
    NodeSpec,
    Placement,
    Step,
    ToolConfig,
)


def compute_cluster_steps(
    test: LoadedTest,
    tool_config: ToolConfig,
    namespace: str,
    pvc: str,
    base_path: str,
    jinja_env: Environment,
    nodes: list[NodeSpec] | None = None,
    models_storage: ModelsStorageConfig | None = None,
    peer_namespace: str = "",
    peer_pvc: str = "",
    peer_base_path: str = "",
    peer_models_storage: ModelsStorageConfig | None = None,
) -> tuple[list[Step], dict[str, list[str]]]:
    if nodes is None:
        nodes = []

    placement = test.placement or Placement()

    # Phase 1 — Node filtering
    eligible = _filter_nodes(nodes, placement.set_requirements)
    if not eligible:
        print(f"  Skipping {test.name} (no nodes meet setRequirements)")
        return [], {}

    # Phase 2 — Set generation
    if placement.set_size > len(eligible):
        raise ValueError(
            f"Test '{test.name}': setSize ({placement.set_size}) exceeds "
            f"eligible node count ({len(eligible)})"
        )

    if placement.set_size > 1 and len(test.spec.dag) != placement.set_size:
        raise ValueError(
            f"Test '{test.name}': setSize ({placement.set_size}) != "
            f"number of DAG steps ({len(test.spec.dag)}). "
            f"When setSize > 1, each DAG step is pinned to a different node"
        )

    gen_func = (
        itertools.permutations
        if placement.set_type == "permutation"
        else itertools.combinations
    )
    all_sets = list(gen_func(eligible, placement.set_size))

    if placement.set_selection == "random":
        selected_sets = [random.choice(all_sets)]
    else:
        if placement.set_cutoff > 0:
            selected_sets = all_sets[: placement.set_cutoff]
        else:
            selected_sets = all_sets

    if len(selected_sets) > 9999:
        raise ValueError(
            f"Too many sets: test '{test.name}' produces "
            f"{len(selected_sets)} sets, maximum is 9999"
        )
    multi_set = len(selected_sets) > 1
    set_mappings: dict[str, list[str]] = {}

    # Phase 3 — Step generation
    steps: list[Step] = []
    for set_idx, node_set in enumerate(selected_sets):
        set_key = f"{set_idx:04d}" if multi_set else ""
        set_mappings[set_key or "0000"] = [n.name for n in node_set]

        # Resource validation per target node
        if placement.set_size == 1:
            validate_node_resources(test, node_set[0], jinja_env)
        else:
            for dag_idx, dag_step in enumerate(test.spec.dag):
                _validate_single_step_resources(
                    test, dag_step, node_set[dag_idx], jinja_env
                )

        _generate_set_steps(
            steps,
            test,
            tool_config,
            namespace,
            pvc,
            base_path,
            jinja_env,
            node_set,
            set_key,
            placement.set_size,
            models_storage=models_storage,
            peer_namespace=peer_namespace,
            peer_pvc=peer_pvc,
            peer_base_path=peer_base_path,
            peer_models_storage=peer_models_storage,
        )

    return steps, set_mappings


def _filter_nodes(
    nodes: list[NodeSpec], requirements: dict[str, Any]
) -> list[NodeSpec]:
    if not requirements:
        return list(nodes)

    eligible = []
    for node in nodes:
        sanity = node.component_validation.sanity.model_dump(by_alias=True)
        meets = True
        for key, required in requirements.items():
            actual = sanity.get(key)
            if actual is None:
                meets = False
                break
            if isinstance(required, (int, float)) and isinstance(actual, (int, float)):
                if actual < required:
                    meets = False
                    break
            elif str(actual) != str(required):
                meets = False
                break
        if meets:
            eligible.append(node)
    return eligible


def _validate_single_step_resources(
    test: LoadedTest,
    dag_step: DAGStep,
    node_spec: NodeSpec,
    jinja_env: Environment,
) -> None:
    if not dag_step.resources:
        return
    requests = dag_step.resources.get("requests", {})
    if not requests:
        return

    from .common import parse_k8s_quantity

    node_spec_dict = node_spec.model_dump(by_alias=True)
    sanity_dict = node_spec.component_validation.sanity.model_dump(by_alias=True)
    render_ctx: dict[str, Any] = {
        "nodeSpec": node_spec_dict,
        "serverConfig": test.spec.server_config,
    }

    for rkey, rval in requests.items():
        resolved = render_string(jinja_env, str(rval), render_ctx)
        demand = parse_k8s_quantity(resolved)
        capacity_raw = sanity_dict.get(rkey)
        if capacity_raw is None:
            continue
        capacity = parse_k8s_quantity(capacity_raw)
        if demand > capacity:
            raise ValueError(
                f"Test '{test.name}' DAG step '{dag_step.name}' on node "
                f"'{node_spec.name}': {rkey} demand ({demand}) exceeds "
                f"capacity ({capacity})"
            )


def _generate_set_steps(
    steps: list[Step],
    test: LoadedTest,
    tc: ToolConfig,
    namespace: str,
    pvc: str,
    base_path: str,
    jinja_env: Environment,
    node_set: tuple[NodeSpec, ...],
    set_key: str,
    set_size: int,
    models_storage: ModelsStorageConfig | None = None,
    peer_namespace: str = "",
    peer_pvc: str = "",
    peer_base_path: str = "",
    peer_models_storage: ModelsStorageConfig | None = None,
) -> None:
    set_segment = f"-{set_key}" if set_key else ""
    step_prefix = f"{test.test_id}-{test.name}{set_segment}"

    services: dict[str, dict[str, Any]] = {}
    has_persistent = False
    has_peer_persistent = False
    has_peer = False
    extra_resource_types: set[str] = set()
    extra_peer_resource_types: set[str] = set()
    scope = "cluster"

    for dag_idx, dag_step in enumerate(test.spec.dag):
        if set_size == 1:
            target_node = node_set[0]
        else:
            target_node = node_set[dag_idx]

        node = target_node.name
        node_spec_dict = target_node.model_dump(by_alias=True)
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
                step_node=set_key,
                test=test,
                tc=tc,
                namespace=step_ns,
                services=services,
                jinja_env=jinja_env,
                scope=scope,
                node_spec_dict=node_spec_dict,
                set_key=set_key,
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
                step_node=set_key,
                test=test,
                tc=tc,
                namespace=step_ns,
                pvc=step_pvc,
                base_path=step_base_path,
                services=services,
                jinja_env=jinja_env,
                scope=scope,
                node_spec_dict=node_spec_dict,
                set_key=set_key,
                models_storage=step_models,
            )
        else:
            chain_label = fit(set_key, 4) if set_key else ""
            sel_extra = f",chain={chain_label}" if chain_label else ""
            add_ephemeral_steps(
                steps,
                dag_step,
                step_prefix,
                node=node,
                step_node=set_key,
                test=test,
                tc=tc,
                namespace=step_ns,
                pvc=step_pvc,
                base_path=step_base_path,
                services=services,
                jinja_env=jinja_env,
                scope=scope,
                selector_extra=sel_extra,
                node_spec_dict=node_spec_dict,
                set_key=set_key,
                models_storage=step_models,
            )

    test_label = fit(test.name, 63)
    chain_label = fit(set_key, 4) if set_key else ""
    selector = (
        f"test={test_label},chain={chain_label}" if set_key else f"test={test_label}"
    )
    add_teardown_steps(
        steps,
        has_persistent,
        step_prefix,
        selector=selector,
        step_node=set_key,
        test=test,
        scope=scope,
        set_key=set_key,
        extra_resource_types=extra_resource_types,
        namespace=namespace,
    )
    if has_peer and peer_namespace:
        add_teardown_steps(
            steps,
            has_peer_persistent,
            f"{step_prefix}-peer",
            selector=selector,
            step_node=set_key,
            test=test,
            scope=scope,
            set_key=set_key,
            extra_resource_types=extra_peer_resource_types,
            namespace=peer_namespace,
        )
