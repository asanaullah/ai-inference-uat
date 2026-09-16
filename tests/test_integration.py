# Assisted by Claude Opus
"""End-to-end integration tests using examples/all_tests.yaml and test_lib."""

import json
import subprocess

import pytest


@pytest.fixture()
def build_dir(tmp_path):
    subprocess.run(
        [
            "python",
            "-m",
            "src",
            "--test-suite",
            "examples/all_tests.yaml",
            "--test-lib",
            "test_lib",
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
        orig_base = build_dir / "manual"
        rt_base = rt_dir / "manual"
        orig_files = sorted(
            f.relative_to(orig_base) for f in orig_base.rglob("*") if f.is_file()
        )
        rt_files = sorted(
            f.relative_to(rt_base) for f in rt_base.rglob("*") if f.is_file()
        )
        assert orig_files == rt_files, (
            f"File list mismatch:\n"
            f"  only in original: {set(orig_files) - set(rt_files)}\n"
            f"  only in roundtrip: {set(rt_files) - set(orig_files)}"
        )
        for rel in orig_files:
            orig_content = (orig_base / rel).read_bytes()
            rt_content = (rt_base / rel).read_bytes()
            assert orig_content == rt_content, f"{rel} differs after round-trip"
