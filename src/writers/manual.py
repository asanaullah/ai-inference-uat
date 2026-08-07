# Assisted by Claude Opus 4.6
"""Manual-run writer: numbered shell scripts and YAML manifests."""

import shutil
import stat
from pathlib import Path

from jinja2 import Environment

from ..common import render_template
from ..models import Step


def write_manual(
    steps: list[Step],
    output_dir: Path,
    run_id: str,
    jinja_env: Environment,
) -> None:
    manual_dir = output_dir / "manual"
    if manual_dir.exists():
        shutil.rmtree(manual_dir)
    manual_dir.mkdir(parents=True)

    setup = [s for s in steps if s.phase == "setup"]
    test_steps = [s for s in steps if s.phase == "test"]
    teardown = [s for s in steps if s.phase == "teardown"]

    pad_width = len(str(len(steps)))

    counter = 1
    for step in setup:
        if _write_step(step, manual_dir, jinja_env, counter, pad_width):
            counter += 1

    tests_grouped: dict[str, list[Step]] = {}
    for s in test_steps:
        tests_grouped.setdefault(s.test_id, []).append(s)

    for t_steps in tests_grouped.values():
        if t_steps[0].scope == "node":
            nodes_grouped: dict[str, list[Step]] = {}
            for s in t_steps:
                nodes_grouped.setdefault(s.node, []).append(s)
            nodes = list(nodes_grouped.keys())
            max_len = max(len(nodes_grouped[n]) for n in nodes)
            for i in range(max_len):
                wrote_any = False
                for n in nodes:
                    if i < len(nodes_grouped[n]) and _write_step(
                        nodes_grouped[n][i],
                        manual_dir,
                        jinja_env,
                        counter,
                        pad_width,
                    ):
                        wrote_any = True
                if wrote_any:
                    counter += 1
        else:
            for s in t_steps:
                if _write_step(s, manual_dir, jinja_env, counter, pad_width):
                    counter += 1

    for step in teardown:
        if _write_step(step, manual_dir, jinja_env, counter, pad_width):
            counter += 1

    _stamp(manual_dir, run_id)


def _step_filename(step: Step) -> str:
    return step.name


def _write_step(
    step: Step,
    directory: Path,
    jinja_env: Environment,
    counter: int,
    pad_width: int,
) -> bool:
    assert step.type in ("generate", "command"), f"Unknown step type: {step.type}"

    if step.type == "generate":
        assert "output" in step.config, (
            f"Generate step {step.name} missing config.output"
        )
        assert step.content, f"Generate step {step.name} has empty content"
        filename = _step_filename(step)
        if step.config["output"] == "manifest":
            manifests_dir = directory / "manifests"
            manifests_dir.mkdir(exist_ok=True)
            path = manifests_dir / f"{filename}.yaml"
            path.write_text(step.content)
            return False
        ext = ".sh"
        path = directory / f"{str(counter).zfill(pad_width)}-{filename}{ext}"
        path.write_text(step.content)
        _make_executable(path)
        return True

    script = _derive_manual_script(step, jinja_env)
    if script:
        filename = _step_filename(step)
        path = directory / f"{str(counter).zfill(pad_width)}-{filename}.sh"
        path.write_text(script)
        _make_executable(path)
        return True
    return False


def _derive_manual_script(step: Step, jinja_env: Environment) -> str | None:
    config = step.config
    assert "command" in config, f"Command step {step.name} missing config.command"
    cmd = config["command"]

    if cmd == "apply":
        source_name = step.source[0] if step.source else ""
        manifest = f"manifests/{source_name}.yaml"
        return render_template(
            jinja_env,
            "apply-script.sh.j2",
            {
                "manifest": manifest,
                "probe": config.get("probe", "none"),
                "pod_name": config.get("pod_name", ""),
                "timeout": config.get("timeout", ""),
            },
        )
    elif cmd == "exec":
        assert "target" in config, f"Exec step {step.name} missing config.target"
        assert "args" in config, f"Exec step {step.name} missing config.args"
        return render_template(
            jinja_env,
            "exec-script.sh.j2",
            {
                "target": config["target"],
                "args": config["args"],
            },
        )
    elif cmd == "delete":
        assert "selector" in config, f"Delete step {step.name} missing config.selector"
        return render_template(
            jinja_env,
            "teardown-script.sh.j2",
            {
                "selector": config["selector"],
                "resource_types": config.get(
                    "resource_types", "pods,services,deployments"
                ),
            },
        )
    elif cmd == "delete-all":
        return render_template(
            jinja_env,
            "cleanup-script.sh.j2",
            {
                "configmap_name": config.get("configmap_name", ""),
                "managed_by_label": config.get("managed_by_label", ""),
            },
        )
    return None


def _stamp(directory: Path, run_id: str) -> None:
    for path in directory.rglob("*"):
        if path.is_file():
            content = path.read_text()
            if "__TIMESTAMP__" in content:
                path.write_text(content.replace("__TIMESTAMP__", run_id))


def _make_executable(path: Path) -> None:
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
