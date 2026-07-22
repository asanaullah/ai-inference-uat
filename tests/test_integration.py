"""End-to-end integration tests using examples/minimal."""

import json
import subprocess

import pytest
import yaml


@pytest.fixture()
def build_dir(tmp_path):
    subprocess.run(
        [
            "python",
            "-m",
            "src",
            "--suite-dir",
            "examples/minimal",
            "--cluster",
            "cluster/ocp-test.yaml",
            "--config",
            "config.yaml",
            "--scripts-dir",
            "scripts",
            "--output",
            str(tmp_path),
        ],
        check=True,
        capture_output=True,
    )
    return tmp_path


class TestOutputStructure:
    def test_manual_layout(self, build_dir):
        manual = build_dir / "manual"
        assert manual.is_dir()
        scripts = [f for f in manual.iterdir() if f.is_file()]
        assert all(f.suffix == ".sh" for f in scripts)
        manifests_dir = manual / "manifests"
        assert manifests_dir.is_dir()
        manifests = list(manifests_dir.iterdir())
        assert all(f.suffix == ".yaml" for f in manifests)

    def test_tekton_dir(self, build_dir):
        tekton = build_dir / "tekton"
        assert (tekton / "cluster-pipeline.yaml").exists()
        assert (tekton / "pipelinerun.yaml").exists()
        assert any(f.name.startswith("task-guard-") for f in tekton.iterdir())

    def test_steps_json(self, build_dir):
        data = json.loads((build_dir / "steps.json").read_text())
        assert "metadata" in data
        assert "steps" in data
        assert isinstance(data["steps"], list)
        assert len(data["steps"]) > 0
        for step in data["steps"]:
            assert "scope" in step


class TestManualOutput:
    def test_setup_files(self, build_dir):
        manual = build_dir / "manual"
        names = [f.name for f in manual.iterdir() if f.is_file()]
        manifests = [f.name for f in (manual / "manifests").iterdir()]
        assert "apply-configmap.yaml" in manifests
        assert "create-builder.yaml" in manifests
        assert any(n.endswith("-apply-configmap.sh") for n in names)
        assert any(n.endswith("-create-builder.sh") for n in names)
        assert any(n.endswith("-build.sh") for n in names)

    def test_teardown_files(self, build_dir):
        manual = build_dir / "manual"
        names = [f.name for f in manual.iterdir() if f.is_file()]
        manifests = [f.name for f in (manual / "manifests").iterdir()]
        assert any("create-aggregator.yaml" in n for n in manifests)
        assert any("aggregate.sh" in n for n in names)
        assert any("cleanup.sh" in n for n in names)

    def test_no_timestamp_placeholder(self, build_dir):
        for f in (build_dir / "manual").rglob("*"):
            if f.is_file():
                assert "__TIMESTAMP__" not in f.read_text(), f"Unsubstituted in {f}"

    def test_scripts_executable(self, build_dir):
        for f in (build_dir / "manual").rglob("*.sh"):
            assert f.stat().st_mode & 0o111, f"{f} not executable"

    def test_script_glob_order(self, build_dir):
        scripts = sorted(f.name for f in (build_dir / "manual").glob("*.sh"))
        counters = [int(s.split("-", 1)[0]) for s in scripts]
        assert counters == sorted(counters)


class TestTektonOutput:
    def test_all_yaml_valid(self, build_dir):
        for f in (build_dir / "tekton").glob("*.yaml"):
            docs = list(yaml.safe_load_all(f.read_text()))
            for doc in docs:
                if doc is None:
                    continue
                assert "apiVersion" in doc, f"Missing apiVersion in {f.name}"
                assert "kind" in doc, f"Missing kind in {f.name}"

    def test_timestamp_uses_param(self, build_dir):
        for f in (build_dir / "tekton").glob("*.yaml"):
            content = f.read_text()
            assert "__TIMESTAMP__" not in content, f"Unsubstituted in {f.name}"

    def test_flat_pipeline_all_task_refs(self, build_dir):
        tekton = build_dir / "tekton"
        doc = yaml.safe_load((tekton / "cluster-pipeline.yaml").read_text())
        for task in doc["spec"]["tasks"]:
            assert "taskRef" in task, f"Task {task['name']} missing taskRef"
            assert "pipelineRef" not in task, (
                f"Task {task['name']} has pipelineRef (expected flat pipeline)"
            )

    def test_guard_tasks_exist(self, build_dir):
        tekton = build_dir / "tekton"
        guard_files = [f for f in tekton.iterdir() if f.name.startswith("task-guard-")]
        assert len(guard_files) > 0, "No guard task files generated"

    def test_no_nested_pipeline_files(self, build_dir):
        tekton = build_dir / "tekton"
        node_pipelines = [f for f in tekton.iterdir() if f.name.startswith("node-")]
        test_pipelines = [
            f
            for f in tekton.iterdir()
            if f.name.startswith("test-") and not f.name.startswith("test-pod")
        ]
        assert len(node_pipelines) == 0, "Node pipeline files should not exist"
        assert len(test_pipelines) == 0, "Test pipeline files should not exist"


class TestStepsRoundTrip:
    def test_roundtrip(self, build_dir, tmp_path):
        rt_dir = tmp_path / "rt"
        subprocess.run(
            [
                "python",
                "-m",
                "src",
                "--steps",
                str(build_dir / "steps.json"),
                "--output",
                str(rt_dir),
            ],
            check=True,
            capture_output=True,
        )
        orig_files = sorted(
            f.relative_to(build_dir / "manual")
            for f in (build_dir / "manual").rglob("*")
            if f.is_file()
        )
        rt_files = sorted(
            f.relative_to(rt_dir / "manual")
            for f in (rt_dir / "manual").rglob("*")
            if f.is_file()
        )
        assert orig_files == rt_files
