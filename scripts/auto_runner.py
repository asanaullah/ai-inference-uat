#!/usr/bin/env python3
# Assisted by Claude Opus
"""
auto_runner.py — Headless driver for UAT test suites.

Runs inside the uat-runner pod (see setup/auto_runner.yaml). Loads the build
directory produced by the generator (steps.json + manual/*.sh) and executes
every lifecycle and test item in order, unattended — the same units and stages
that scripts/manual_runner.py exposes interactively, but with no UI.

    python3 scripts/auto_runner.py <build-dir> \
        [--logs <dir>] [--run-id <id>] [--no-preflight]

Preflight
---------
Before any steps run, the test namespaces (project + peer) are cleared so
artifacts from an earlier run can't leak into this one — especially when the
run-id is not unique. For each namespace it deletes all uat resources (pods,
services, configmaps, and the CRD instances the suites create), then launches a
short-lived pod that mounts the storage PVC at its root and removes this run's
results dir (<base_path>/<run_id>) if it is non-empty. Only that run's subtree
is removed, so other runs' artifacts under base_path are left intact.

Execution model
---------------
Each step's generated bash script is still run via subprocess (the scripts wrap
`oc apply`, waits, and pass/fail checks). What changes versus manual_runner.py
is observability: pod logs/phase and, on failure, pod container states and
namespace events are captured through the kubernetes client (in-cluster config,
i.e. the uat-runner-sa token). Failure handling is driven entirely by each
step's `on_failure` policy from the suite (continue / skipTest / abort).

Outputs (under --logs, default <build-dir>/logs)
------------------------------------------------
    preflight/teardown-<ns>.log                 resources deleted per namespace
    preflight/clean-<ns>.log                    storage cleaner pod output
    <item-id>_<name>_<run>/shell/<script>.log   stdout/stderr of each script
    <item-id>_<name>_<run>/pod/<pod>.log        pod logs via the k8s client
    <item-id>_<name>_<run>/pod/<script>.diag.txt pod state + events on failure
    timesheet.csv                               one row per executed step
    status.json                                 machine-readable run summary
"""

import argparse
import csv
import json
import re
import subprocess
import sys
import threading
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path

from kubernetes import client, config, watch
from kubernetes.client.rest import ApiException

sys.path.insert(0, str(Path(__file__).resolve().parent))
from manual_runner import (
    TIMESHEET_HEADER,
    Build,
    Item,
    Stage,
    StepState,
    _slug,
)

# RFC3339 timestamp that `--timestamps` prepends to each log line, used to
# resume after a dropped stream without duplicating lines.
_TS_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T[\d:.]+Z$")

# Namespaced CRD instances the suites create, cleared during preflight.
# (group, version, plural)
_CRD_TYPES = [
    ("jobset.x-k8s.io", "v1alpha2", "jobsets"),
    ("kubeflow.org", "v1", "pytorchjobs"),
    ("kubeflow.org", "v1", "tfjobs"),
    ("kubeflow.org", "v1", "mpijobs"),
    ("kubeflow.org", "v1", "xgboostjobs"),
    ("serving.kserve.io", "v1beta1", "inferenceservices"),
    ("serving.kserve.io", "v1alpha1", "servingruntimes"),
    ("ray.io", "v1", "rayjobs"),
    ("ray.io", "v1", "rayclusters"),
    ("ray.io", "v1", "rayservices"),
]

# Platform-injected configmaps to keep during preflight — cluster CA bundles and
# the ODH/OpenShift-AI trust bundles, none of which are uat run artifacts.
_KEEP_CONFIGMAPS = {
    "kube-root-ca.crt",
    "openshift-service-ca.crt",
    "odh-trusted-ca-bundle",
    "odh-kserve-custom-ca-bundle",
}

_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)

# manual_runner groups every lifecycle item ahead of the tests for its
# interactive picker, but an unattended run needs true execution order:
# set up, run the tests, then tear down. These lifecycle ids run after the
# tests; every other lifecycle id runs before them.
_POST_TEST_LIFECYCLE = ("aggregate", "cleanup")


def _now() -> datetime:
    return datetime.now(timezone.utc)


class AutoRunner:
    """Executes all of a build's items sequentially, streaming to disk."""

    def __init__(self, build: Build, logs_root: Path, run_id: str):
        self.build = build
        self.logs_root = logs_root
        # The build's run_id (`--run-id`, default "manual-run"): results for this
        # run land under <base_path>/<run_id> in each test namespace's storage,
        # so preflight cleans exactly that subtree and leaves other runs intact.
        self.run_id = run_id
        self.logs_root.mkdir(parents=True, exist_ok=True)
        self._core = client.CoreV1Api()
        self._custom = client.CustomObjectsApi()
        self._stop = threading.Event()
        self._ts_path = self.logs_root / "timesheet.csv"
        self._ts_lock = threading.Lock()
        self._followers: list[threading.Thread] = []
        # Per-item outcome, collected into status.json at the end.
        self.results: list[dict] = []
        self.preflight_summary: dict = {}

    # -- top level ---------------------------------------------------------

    def run_all(self, preflight: bool = True) -> bool:
        """Run every item in order. Returns True if all items passed."""
        self._ensure_timesheet()
        if preflight:
            try:
                self.preflight()
            except Exception:  # noqa: BLE001  never let cleanup abort the run
                trace = traceback.format_exc()
                print(f"[auto-runner] preflight error:\n{trace}", flush=True)
                self.preflight_summary["error"] = trace

        all_ok = True
        aborting = False
        post_ids = set(_POST_TEST_LIFECYCLE)
        for item in self._ordered_items():
            # Once a test has hit an `abort` policy we skip straight to cleanup:
            # only the post-test lifecycle (aggregate/cleanup) still runs, every
            # remaining test is skipped.
            if aborting and item.item_id not in post_ids:
                print(
                    f"[auto-runner] aborting; skipping {item.item_id} '{item.name}'",
                    flush=True,
                )
                continue
            ok, aborted = self._run_item(item)
            all_ok = all_ok and ok
            if not aborted:
                continue
            # A setup lifecycle step (configmap/build) has no failure policy; if
            # it aborts, nothing downstream can run meaningfully, so stop now
            # without even attempting teardown.
            if item.kind == "lifecycle" and item.item_id not in post_ids:
                print(
                    f"[auto-runner] lifecycle item '{item.name}' aborted; stopping run",
                    flush=True,
                )
                break
            # A test's `abort` policy (or an aborted post-test lifecycle step)
            # skips straight to cleanup: stop launching tests, but let the
            # remaining teardown items run.
            print(
                f"[auto-runner] '{item.name}' aborted; skipping to cleanup",
                flush=True,
            )
            aborting = True
        self._write_status(all_ok)
        return all_ok

    def _ordered_items(self) -> list[Item]:
        """Items in execution order: setup lifecycle, then tests, then teardown
        lifecycle. manual_runner lists all lifecycle items first (for its picker),
        which would otherwise run aggregate/cleanup before any test."""
        tests = [i for i in self.build.items if i.kind == "test"]
        post = [i for i in self.build.items if i.item_id in _POST_TEST_LIFECYCLE]
        post_ids = set(_POST_TEST_LIFECYCLE)
        pre = [
            i
            for i in self.build.items
            if i.kind != "test" and i.item_id not in post_ids
        ]
        return pre + tests + post

    # -- preflight cleanup -------------------------------------------------

    def preflight(self) -> None:
        cs = self.build.cs
        pre_dir = self.logs_root / "preflight"
        pre_dir.mkdir(parents=True, exist_ok=True)

        targets = [(cs.namespace, cs.storage.pvc, cs.storage.base_path)]
        if cs.peer_namespace:
            ps = cs.peer_storage or cs.storage
            targets.append((cs.peer_namespace, ps.pvc, ps.base_path))

        ns_summaries = []
        for ns, pvc, base in targets:
            print(f"[auto-runner] preflight: clearing namespace {ns}", flush=True)
            deleted = self._teardown_namespace(ns, pre_dir)
            cleaned = self._clean_storage(ns, pvc, base, pre_dir)
            ns_summaries.append(
                {"namespace": ns, "deleted": deleted, "storage_cleaned": cleaned}
            )
        self.preflight_summary["namespaces"] = ns_summaries

    def _teardown_namespace(self, ns: str, log_dir: Path) -> int:
        """Delete all uat resources in a namespace. Returns count deleted."""
        deleted = 0
        with open(log_dir / f"teardown-{ns}.log", "w", encoding="utf-8") as log:

            def emit(msg: str) -> None:
                log.write(msg + "\n")
                log.flush()
                print(f"[preflight:{ns}] {msg}", flush=True)

            # CRD instances first — they own pods that would otherwise respawn.
            for group, version, plural in _CRD_TYPES:
                try:
                    items = self._custom.list_namespaced_custom_object(
                        group, version, ns, plural
                    ).get("items", [])
                except ApiException as e:
                    if e.status not in (403, 404):
                        emit(f"list {plural}: {e.status} {e.reason}")
                    continue
                for it in items:
                    name = it.get("metadata", {}).get("name", "")
                    try:
                        self._custom.delete_namespaced_custom_object(
                            group, version, ns, plural, name
                        )
                        deleted += 1
                        emit(f"deleted {plural}/{name}")
                    except ApiException as e:
                        emit(f"delete {plural}/{name}: {e.status} {e.reason}")

            deleted += self._delete_each(
                emit,
                "pod",
                lambda: self._core.list_namespaced_pod(ns).items,
                lambda n: self._core.delete_namespaced_pod(n, ns),
            )
            deleted += self._delete_each(
                emit,
                "service",
                lambda: self._core.list_namespaced_service(ns).items,
                lambda n: self._core.delete_namespaced_service(n, ns),
            )
            deleted += self._delete_each(
                emit,
                "configmap",
                lambda: self._core.list_namespaced_config_map(ns).items,
                lambda n: self._core.delete_namespaced_config_map(n, ns),
                skip=_KEEP_CONFIGMAPS,
            )
        return deleted

    @staticmethod
    def _delete_each(emit, kind, lister, deleter, skip=frozenset()) -> int:
        deleted = 0
        try:
            items = lister()
        except ApiException as e:
            emit(f"list {kind}s: {e.status} {e.reason}")
            return 0
        for obj in items:
            name = obj.metadata.name
            if name in skip:
                continue
            try:
                deleter(name)
                deleted += 1
                emit(f"deleted {kind}/{name}")
            except ApiException as e:
                emit(f"delete {kind}/{name}: {e.status} {e.reason}")
        return deleted

    def _clean_storage(self, ns: str, pvc: str, base_path: str, log_dir: Path) -> bool:
        """Launch a pod that mounts the storage PVC at root and removes anything
        left under base_path from earlier runs. Returns True if it completed."""
        pod_name = "uat-preflight-clean"
        image = self.build.tc.aggregator_image
        # Only this run's results dir, never the whole base_path (which holds
        # other runs' artifacts).
        target = f"/uat_workspace/{base_path}/{self.run_id}"
        script = (
            "set -e; "
            f'd="{target}"; '
            'if [ -d "$d" ] && [ -n "$(ls -A "$d" 2>/dev/null)" ]; then '
            'echo "non-empty, removing:"; ls -la "$d"; rm -rf "$d"; '
            'echo "cleaned $d"; '
            'else echo "$d empty or absent, nothing to clean"; fi'
        )
        pod = client.V1Pod(
            metadata=client.V1ObjectMeta(
                name=pod_name,
                labels={"app.kubernetes.io/managed-by": self.build.tc.managed_by_label},
            ),
            spec=client.V1PodSpec(
                restart_policy="Never",
                containers=[
                    client.V1Container(
                        name="clean",
                        image=image,
                        command=["sh", "-c", script],
                        volume_mounts=[
                            client.V1VolumeMount(
                                name="workspace", mount_path="/uat_workspace"
                            )
                        ],
                    )
                ],
                volumes=[
                    client.V1Volume(
                        name="workspace",
                        persistent_volume_claim=(
                            client.V1PersistentVolumeClaimVolumeSource(claim_name=pvc)
                        ),
                    )
                ],
            ),
        )
        # Remove any stale cleaner pod from a prior aborted preflight.
        try:
            self._core.delete_namespaced_pod(pod_name, ns)
            self._wait_gone(pod_name, ns)
        except ApiException:
            pass
        try:
            self._core.create_namespaced_pod(ns, pod)
        except ApiException as e:
            print(
                f"[preflight:{ns}] create cleaner pod: {e.status} {e.reason}",
                flush=True,
            )
            return False

        phase = self._wait_terminal(pod_name, ns, timeout=300)
        try:
            logs = self._core.read_namespaced_pod_log(name=pod_name, namespace=ns)
        except ApiException:
            logs = ""
        (log_dir / f"clean-{ns}.log").write_text(logs or "", encoding="utf-8")
        print(f"[preflight:{ns}] storage clean phase={phase}", flush=True)
        try:
            self._core.delete_namespaced_pod(pod_name, ns)
        except ApiException:
            pass
        return phase == "Succeeded"

    # -- item / stage execution -------------------------------------------

    def _run_item(self, item: Item) -> tuple[bool, bool]:
        run_stamp = _now().strftime("%Y%m%d_%H%M%S")
        log_dir = self.logs_root / f"{item.item_id}_{_slug(item.name)}_{run_stamp}"
        shell_dir = log_dir / "shell"
        pod_dir = log_dir / "pod"
        shell_dir.mkdir(parents=True, exist_ok=True)
        pod_dir.mkdir(parents=True, exist_ok=True)

        print(f"[auto-runner] === {item.item_id} {item.name} ===", flush=True)
        stages = self.build.stages_for(item)

        failed = False  # any step failed (drives the item's pass/fail)
        halt = False  # a non-continue failure -> skip remaining normal stages
        aborted = False  # an `abort` policy (or lifecycle failure) stops the run
        failed_steps: list[str] = []
        for stage in stages:
            if halt and not stage.is_finally:
                for _, entry in stage.entries:
                    self._record(
                        item, run_stamp, entry.path.name, "SKIPPED", None, None
                    )
                continue
            s_failed, s_halt, s_abort = self._run_stage(
                item, run_stamp, stage, shell_dir, pod_dir, failed_steps
            )
            if not stage.is_finally:
                failed = failed or s_failed
                halt = halt or s_halt
                aborted = aborted or s_abort

        self._stop_followers()
        status = "FAILED" if failed else "PASSED"
        print(
            f"[auto-runner] {item.item_id} {item.name}: {status}"
            + (f" (failed: {', '.join(failed_steps)})" if failed_steps else ""),
            flush=True,
        )
        self.results.append(
            {
                "item_id": item.item_id,
                "name": item.name,
                "kind": item.kind,
                "run": run_stamp,
                "status": status,
                "failed_steps": failed_steps,
                "log_dir": log_dir.name,
            }
        )
        return (not failed), aborted

    def _run_stage(
        self,
        item: Item,
        run_stamp: str,
        stage: Stage,
        shell_dir: Path,
        pod_dir: Path,
        failed_steps: list[str],
    ) -> tuple[bool, bool, bool]:
        """Run a stage's entries concurrently. Returns (failed, halt, abort)."""
        procs = []
        for step, entry in stage.entries:
            st = StepState(
                seq=entry.seq,
                label=entry.path.name,
                log_path=shell_dir / f"{entry.path.stem}.log",
                persistent=step.config.get("probe") == "wait-ready",
            )
            st.started = _now()
            print(f"[step START] {st.label} (ns={step.namespace or '-'})", flush=True)
            log_file = open(st.log_path, "w", encoding="utf-8")  # noqa: SIM115
            proc = subprocess.Popen(
                ["bash", str(entry.path)],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                cwd=str(entry.path.parent),
            )
            reader = threading.Thread(
                target=self._stream,
                args=(proc, log_file, entry.path.stem),
                daemon=True,
            )
            reader.start()
            # Attach the pod follower now, while the pod is still alive, so a
            # run-to-completion pod isn't deleted by the next stage before its
            # logs drain.
            self._maybe_follow_pod(step, pod_dir)
            procs.append((step, entry, st, proc, log_file, reader))

        failed = halt = abort = False
        for step, entry, st, proc, log_file, reader in procs:
            proc.wait()
            reader.join(timeout=1)
            log_file.close()
            finished = _now()
            dur = (finished - st.started).total_seconds() if st.started else 0.0
            status = "OK" if proc.returncode == 0 else "FAILED"
            if proc.returncode == 0:
                print(f"[step OK]    {st.label} ({dur:.1f}s)", flush=True)
            else:
                failed = True
                # A finally/teardown failure is surfaced in its own log but does
                # not mark the item failed, so keep it out of failed_steps.
                if not stage.is_finally:
                    failed_steps.append(st.label)
                print(
                    f"[step FAILED] {st.label} rc={proc.returncode} "
                    f"({dur:.1f}s) — detail: {st.log_path}",
                    flush=True,
                )
                # Snapshot pod state + namespace events while they're fresh.
                self._capture_diagnostics(
                    step, pod_dir / f"{entry.path.stem}.diag.txt", proc.returncode
                )
                policy = step.on_failure  # "continue"|"skipTest"|"abort"|""
                if policy != "continue":
                    halt = True
                if policy == "abort" or not policy:
                    abort = True
            self._record(item, run_stamp, st.label, status, st.started, finished)
        return failed, halt, abort

    # -- diagnostics -------------------------------------------------------

    def _capture_diagnostics(self, step, out_path: Path, rc: int) -> None:
        ns = step.namespace or ""
        pod = step.config.get("pod_name")
        lines = [
            f"step: {step.name}",
            f"namespace: {ns}",
            f"return_code: {rc}",
            f"captured: {_now().isoformat()}",
            "",
        ]
        if pod:
            lines.append(f"=== pod/{pod} ===")
            try:
                p = self._core.read_namespaced_pod(pod, ns)
                lines.append(f"phase: {p.status.phase}")
                for c in p.status.conditions or []:
                    lines.append(
                        f"condition {c.type}={c.status} {c.reason or ''} "
                        f"{c.message or ''}".rstrip()
                    )
                for cs_ in p.status.container_statuses or []:
                    state = cs_.state
                    if state.waiting:
                        detail = (
                            f"waiting: {state.waiting.reason} "
                            f"{state.waiting.message or ''}"
                        )
                    elif state.terminated:
                        detail = (
                            f"terminated: exit={state.terminated.exit_code} "
                            f"{state.terminated.reason or ''} "
                            f"{state.terminated.message or ''}"
                        )
                    elif state.running:
                        detail = "running"
                    else:
                        detail = "unknown"
                    lines.append(
                        f"container {cs_.name}: ready={cs_.ready} "
                        f"restarts={cs_.restart_count} {detail}".rstrip()
                    )
            except ApiException as e:
                lines.append(f"read pod: {e.status} {e.reason}")
            lines.append("")

        lines.append(f"=== events (namespace {ns}) ===")
        try:
            evs = sorted(
                self._core.list_namespaced_event(ns).items,
                key=lambda e: e.last_timestamp or e.event_time or _EPOCH,
            )
            for e in evs[-50:]:
                ts = e.last_timestamp or e.event_time
                obj = e.involved_object
                who = f"{obj.kind}/{obj.name}" if obj else "?"
                lines.append(f"{ts} {e.type} {e.reason} {who}: {e.message}")
        except ApiException as e:
            lines.append(f"list events: {e.status} {e.reason}")

        try:
            out_path.write_text("\n".join(lines), encoding="utf-8")
        except OSError:
            pass

    # -- pod log capture (kubernetes client) -------------------------------

    def _maybe_follow_pod(self, step, pod_dir: Path) -> None:
        if self._stop.is_set() or step.config.get("command") != "apply":
            return
        pod = step.config.get("pod_name")
        if not pod:
            return
        ns = step.namespace or ""
        deadline = time.monotonic() + float(step.config.get("timeout", 600))
        print(f"  [pod] following {ns}/{pod} -> {pod_dir / (pod + '.log')}", flush=True)
        t = threading.Thread(
            target=self._follow_pod,
            daemon=True,
            args=(pod, ns, pod_dir / f"{pod}.log", deadline),
        )
        self._followers.append(t)
        t.start()

    def _follow_pod(self, pod: str, ns: str, log_path: Path, deadline: float) -> None:
        # Wait for the container to start; the API rejects a log stream on a
        # still-Pending pod ("waiting to start: ContainerCreating").
        while not self._stop.is_set() and time.monotonic() < deadline:
            if self._pod_phase(pod, ns) in ("Running", "Succeeded", "Failed"):
                break
            time.sleep(0.5)
        else:
            return

        try:
            log_file = open(log_path, "w", encoding="utf-8")  # noqa: SIM115
        except OSError:
            return
        last_ts = ""
        try:
            while not self._stop.is_set():
                last_ts = self._drain_log(pod, ns, log_file, last_ts)
                # Reconnect only while the pod is still going; the hosted control
                # plane drops long/idle streams, so a Running pod means resume.
                if self._stop.is_set() or self._pod_phase(pod, ns) not in (
                    "Running",
                    "Pending",
                ):
                    break
                time.sleep(1.0)
        finally:
            log_file.close()

    def _drain_log(self, pod: str, ns: str, log_file, last_ts: str) -> str:
        """Stream one log connection, stripping/using the RFC3339 timestamp so a
        reconnect resumes instead of truncating or duplicating. Returns the
        latest timestamp seen."""
        kwargs = {
            "name": pod,
            "namespace": ns,
            "follow": True,
            "timestamps": True,
            "_preload_content": False,
        }
        if last_ts:
            # since_seconds is coarse; per-line ts<=last_ts dedupe below covers
            # any overlap it pulls back in.
            try:
                delta = _now() - datetime.fromisoformat(last_ts.replace("Z", "+00:00"))
                kwargs["since_seconds"] = max(1, int(delta.total_seconds()) + 1)
            except ValueError:
                pass
        latest = last_ts
        w = watch.Watch()
        try:
            for line in w.stream(self._core.read_namespaced_pod_log, **kwargs):
                ts, sep, rest = line.partition(" ")
                if sep and _TS_RE.match(ts):
                    if last_ts and ts <= last_ts:
                        continue
                    latest = max(latest, ts)
                    content = rest
                else:
                    content = line
                log_file.write(content + "\n")
                log_file.flush()
                if self._stop.is_set():
                    break
        except (ApiException, OSError):
            pass
        finally:
            w.stop()
        return latest

    def _pod_phase(self, pod: str, ns: str) -> str:
        # Read the pod object (needs only `get pods`), not read_namespaced_pod_
        # status, which hits the pods/status subresource the runner SA can't get.
        try:
            p = self._core.read_namespaced_pod(name=pod, namespace=ns)
        except (ApiException, OSError):
            return ""
        return p.status.phase or ""

    def _wait_terminal(self, pod: str, ns: str, timeout: float = 300) -> str:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            phase = self._pod_phase(pod, ns)
            if phase in ("Succeeded", "Failed"):
                return phase
            time.sleep(2)
        return "Timeout"

    def _wait_gone(self, pod: str, ns: str, timeout: float = 60) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                self._core.read_namespaced_pod(name=pod, namespace=ns)
            except ApiException:
                return
            time.sleep(1)

    def _stop_followers(self) -> None:
        for t in self._followers:
            t.join(timeout=2)
        self._followers.clear()

    # -- io helpers --------------------------------------------------------

    @staticmethod
    def _stream(proc: subprocess.Popen, log_file, prefix: str) -> None:
        # Write each line to the step's log file and mirror it to the pod's
        # stdout (prefixed) so `oc logs -f uat-runner` shows what each step is
        # doing live. Concurrent steps in a stage interleave; the prefix keeps
        # their lines attributable.
        assert proc.stdout is not None
        for raw in iter(proc.stdout.readline, b""):
            text = raw.decode("utf-8", errors="replace")
            log_file.write(text)
            log_file.flush()
            print(f"  [{prefix}|out] {text.rstrip()}", flush=True)
        try:
            proc.stdout.close()
        except OSError:
            pass

    def _ensure_timesheet(self) -> None:
        with self._ts_lock:
            if not self._ts_path.exists():
                with open(self._ts_path, "w", newline="", encoding="utf-8") as f:
                    csv.writer(f).writerow(TIMESHEET_HEADER)

    def _record(
        self,
        item: Item,
        run_stamp: str,
        label: str,
        status: str,
        started: datetime | None,
        finished: datetime | None,
    ) -> None:
        dur = ""
        if started and finished:
            dur = f"{(finished - started).total_seconds():.1f}"
        row = [
            run_stamp,
            item.item_id,
            item.name,
            label,
            status,
            started.isoformat(sep=" ", timespec="seconds") if started else "",
            finished.isoformat(sep=" ", timespec="seconds") if finished else "",
            dur,
        ]
        with self._ts_lock, open(self._ts_path, "a", newline="", encoding="utf-8") as f:
            csv.writer(f).writerow(row)

    def _write_status(self, all_ok: bool) -> None:
        summary = {
            "finished": _now().isoformat(),
            "overall": "PASSED" if all_ok else "FAILED",
            "preflight": self.preflight_summary,
            "items": self.results,
        }
        (self.logs_root / "status.json").write_text(json.dumps(summary, indent=2))


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Headless UAT runner (steps.json + manual/*.sh)."
    )
    parser.add_argument("build_dir", help="build directory (steps.json + manual/)")
    parser.add_argument(
        "--logs", default=None, help="log output dir (default <build-dir>/logs)"
    )
    parser.add_argument(
        "--run-id",
        default=None,
        help="build run-id; preflight cleans <base_path>/<run-id> in each test "
        "namespace. Defaults to the build's own run_id.txt, or 'manual-run' if "
        "that file is absent.",
    )
    parser.add_argument(
        "--no-preflight",
        action="store_true",
        help="skip clearing the test namespaces before running",
    )
    args = parser.parse_args()

    build = Build(Path(args.build_dir))
    logs_root = Path(args.logs) if args.logs else build.build_dir / "logs"

    # The build stamps its scripts with a run_id and records it in run_id.txt;
    # prefer that so we clean exactly the results dir this build wrote to. An
    # explicit --run-id overrides it; fall back to the shared default only if
    # neither is available.
    run_id = args.run_id
    if run_id is None:
        run_id_file = build.build_dir / "run_id.txt"
        run_id = (
            run_id_file.read_text().strip() if run_id_file.exists() else "manual-run"
        )

    # In-cluster: authenticate as the pod's ServiceAccount (uat-runner-sa).
    config.load_incluster_config()

    runner = AutoRunner(build, logs_root, run_id)
    ok = runner.run_all(preflight=not args.no_preflight)
    print(f"[auto-runner] overall: {'PASSED' if ok else 'FAILED'}", flush=True)
    raise SystemExit(0 if ok else 1)


if __name__ == "__main__":
    main()
