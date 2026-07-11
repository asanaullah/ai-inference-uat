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
    def test_manual_dirs(self, build_dir):
        assert (build_dir / "manual" / "setup").is_dir()
        assert (build_dir / "manual" / "teardown").is_dir()
        for d in (build_dir / "manual" / "nodes").iterdir():
            assert d.is_dir()

    def test_tekton_dir(self, build_dir):
        tekton = build_dir / "tekton"
        assert (tekton / "cluster-pipeline.yaml").exists()
        assert (tekton / "pipelinerun.yaml").exists()
        assert any(f.name.startswith("node-pipeline-") for f in tekton.iterdir())

    def test_steps_json(self, build_dir):
        data = json.loads((build_dir / "steps.json").read_text())
        assert "metadata" in data
        assert "setup" in data
        assert "nodes" in data
        assert "teardown" in data


class TestManualOutput:
    def test_setup_files(self, build_dir):
        setup = build_dir / "manual" / "setup"
        assert (setup / "configmap.yaml").exists()
        assert (setup / "builder-pod.yaml").exists()
        assert (setup / "build.sh").exists()

    def test_teardown_files(self, build_dir):
        td = build_dir / "manual" / "teardown"
        assert (td / "aggregator-pod.yaml").exists()
        assert (td / "aggregate.sh").exists()
        assert (td / "cleanup.sh").exists()

    def test_no_timestamp_placeholder(self, build_dir):
        for f in (build_dir / "manual").rglob("*"):
            if f.is_file():
                assert "__TIMESTAMP__" not in f.read_text(), f"Unsubstituted in {f}"

    def test_scripts_executable(self, build_dir):
        for f in (build_dir / "manual").rglob("*.sh"):
            assert f.stat().st_mode & 0o111, f"{f} not executable"


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
