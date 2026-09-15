#!/usr/bin/env python3
# Assisted by Claude Opus
"""
manual_runner.py — Curses dashboard for running UAT test suites manually.

Loads the build directory produced by the UAT generator (steps.json + bash
scripts under manual/) and presents a full-screen, colored dashboard for
running lifecycle steps and tests individually against a live cluster.

Usage:
    python3 scripts/manual_runner.py <build-dir>

    <build-dir>     directory containing steps.json and manual/*.sh

===============================================================================
LAYOUT
===============================================================================

    ┌ LIFECYCLE / TESTS ─┐┌ STEPS ───────────────────────────┐
    │ > configmap        ││  001-apply-configmap.sh    OK     │
    │   builds           ││  003-build.sh              RUNNING│
    │   aggregate        │├ LOG ──────────────────────────────┤
    │   final cleanup    ││  pod/uat-builder created           │
    │                    ││  waiting for ready ...             │
    │   t1 platform-check││  ...                               │
    │   t2 component     ││                                    │
    └────────────────────┘└───────────────────────────────────┘
     ↑/↓ select   Enter run   q quit

Left pane  — navigable list. Two sections: LIFECYCLE (configmap, builds,
             aggregate, final cleanup) and TESTS (one row per test). Move the
             selection with the Up/Down arrow keys.
Right pane — for the item currently running (or last run): the status of every
             step in that item (PENDING / RUNNING / OK / FAILED / SKIPPED) on
             top, and a live tail of the streaming log below.

===============================================================================
ITEMS
===============================================================================

Lifecycle items group the global (test-less) steps from steps.json by name:
    configmap       apply-configmap, apply-peer-configmap
    builds          create-builder, build, create-peer-builder, peer-build
    aggregate       create-aggregator, aggregate, create-peer-*aggregat*
    final cleanup   cleanup, peer-cleanup

Test items are one per test (test_id / name), containing that test's command
steps in execution order. Steps that share a sequence number run in parallel.
finally_step steps run last and always run, even if an earlier step failed.

===============================================================================
LOGS
===============================================================================

A logs/ directory is created inside the build dir. Each run of an item writes
into <build-dir>/logs/<item-id>_<item-name>_<run-time>/, timestamped at the
moment of the run so a rerun never overwrites an earlier run's logs. It is split
into two subdirectories:

    shell/<script-name>.log   stdout/stderr of each bash script (oc apply,
                              waits, pass/fail checks, etc.)
    pod/<pod-name>.log        the pod's own logs, captured via `oc logs -f`.
                              Started once a pod's apply step finishes: a
                              completed job pod dumps its full log and exits;
                              a persistent pod (e.g. vLLM server) streams until
                              teardown deletes it or the run ends.

Log files are flushed after every line so output is persisted to disk as it is
produced (no waiting on buffer overflow), which also survives a crash or kill.

A logs/timesheet.csv is appended across all runs, one row per executed step:
run, test_id, test_name, step, status, started, finished, duration_s.

===============================================================================
REUSABLE CODE FROM src/
===============================================================================

step_generator.load_steps_file(path)
    Parses steps.json, validates it via StepsFile, returns
    (list[Step], ToolConfig, ClusterTestSpec).

models.Step / ToolConfig / ClusterTestSpec
    Deserialized steps.json entries and cluster/tool metadata.
"""

import argparse
import csv
import curses
import re
import subprocess
import sys
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from src.models import ClusterTestSpec, Step, ToolConfig
from src.step_generator import load_steps_file

# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

# Ordered lifecycle groups. Each entry is (id, display name, name matcher).
# A global step is placed in the first group whose matcher returns True.
LIFECYCLE_GROUPS: list[tuple[str, str]] = [
    ("configmap", "configmap"),
    ("builds", "builds"),
    ("aggregate", "aggregate"),
    ("cleanup", "final cleanup"),
]


def _classify_lifecycle(step_name: str) -> str | None:
    n = step_name.lower()
    if "configmap" in n:
        return "configmap"
    if "build" in n:  # create-builder, build, create-peer-builder, peer-build
        return "builds"
    if "aggregat" in n:  # create-aggregator, aggregate, peer variants
        return "aggregate"
    if "cleanup" in n:
        return "cleanup"
    return None


@dataclass
class ScriptEntry:
    seq: int
    path: Path
    step_name: str


@dataclass
class Item:
    """A selectable, runnable unit in the left pane."""

    item_id: str  # "configmap", "t1", ...
    name: str  # "configmap", "platform-check", ...
    kind: str  # "lifecycle" or "test"
    scope: str  # "", "project", "node", "cluster"
    steps: list[Step] = field(default_factory=list)


@dataclass
class Stage:
    """One or more (step, script) pairs that run together (same seq)."""

    seq: int
    entries: list[tuple[Step, ScriptEntry]]
    is_finally: bool


@dataclass
class StepState:
    seq: int
    label: str  # script filename
    log_path: Path
    status: str = "PENDING"  # PENDING/RUNNING/OK/FAILED/SKIPPED
    rc: int | None = None
    has_pod: bool = False
    persistent: bool = False  # pod reaches Ready but never completes (e.g. vLLM)
    started: datetime | None = None
    shell_log: deque[str] = field(default_factory=lambda: deque(maxlen=2000))
    pod_log: deque[str] = field(default_factory=lambda: deque(maxlen=2000))


# ---------------------------------------------------------------------------
# Build model — parse steps.json into items + script index
# ---------------------------------------------------------------------------


class Build:
    def __init__(self, build_dir: Path):
        # Resolve to an absolute path so scripts launched with a changed cwd
        # still reference the correct files (relative paths break otherwise).
        build_dir = build_dir.resolve()
        self.build_dir = build_dir
        steps_path = build_dir / "steps.json"
        manual_dir = build_dir / "manual"
        if not steps_path.exists():
            raise SystemExit(f"Error: {steps_path} not found")
        if not manual_dir.is_dir():
            raise SystemExit(f"Error: {manual_dir} not found")

        steps, tc, cs = load_steps_file(steps_path)
        self.steps: list[Step] = steps
        self.tc: ToolConfig = tc
        self.cs: ClusterTestSpec = cs

        # Index scripts by step name.
        self.scripts: dict[str, list[ScriptEntry]] = {}
        for f in sorted(manual_dir.glob("*.sh")):
            m = re.match(r"(\d+)-(.+)\.sh$", f.name)
            if not m:
                continue
            entry = ScriptEntry(seq=int(m.group(1)), path=f, step_name=m.group(2))
            self.scripts.setdefault(entry.step_name, []).append(entry)

        self.items: list[Item] = self._build_items()

    def _build_items(self) -> list[Item]:
        # Lifecycle groups (global, test-less steps).
        groups: dict[str, Item] = {
            gid: Item(item_id=gid, name=name, kind="lifecycle", scope="")
            for gid, name in LIFECYCLE_GROUPS
        }
        tests: dict[str, Item] = {}

        for s in self.steps:
            if not s.test:
                gid = _classify_lifecycle(s.name)
                if gid is not None:
                    groups[gid].steps.append(s)
                continue
            if s.test not in tests:
                tests[s.test] = Item(
                    item_id=s.test_id, name=s.test, kind="test", scope=s.scope
                )
            tests[s.test].steps.append(s)

        ordered: list[Item] = [
            groups[gid] for gid, _ in LIFECYCLE_GROUPS if groups[gid].steps
        ]
        ordered += sorted(
            tests.values(),
            key=lambda t: (
                int(re.match(r"t(\d+)", t.item_id).group(1))
                if re.match(r"t(\d+)", t.item_id)
                else 0
            ),
        )
        return ordered

    def stages_for(self, item: Item) -> list[Stage]:
        """Ordered stages for an item: normal steps first, finally steps last.

        Steps sharing a sequence number are grouped into one stage and run in
        parallel; finally steps always run even after a failure.
        """
        normal: list[tuple[Step, ScriptEntry]] = []
        finals: list[tuple[Step, ScriptEntry]] = []
        for s in item.steps:
            if s.type != "command":
                continue
            for e in self.scripts.get(s.name, []):
                (finals if s.finally_step else normal).append((s, e))

        return self._group_by_seq(normal, False) + self._group_by_seq(finals, True)

    @staticmethod
    def _group_by_seq(
        pairs: list[tuple[Step, ScriptEntry]], is_finally: bool
    ) -> list[Stage]:
        # Group by sequence number across the whole list (not just adjacent
        # entries): steps.json lists all of one node's steps before the next
        # node's, but a logical step shares one seq across nodes, so grouping
        # by seq value runs both nodes' matching step together in one stage.
        by_seq: dict[int, list[tuple[Step, ScriptEntry]]] = {}
        for step, entry in pairs:
            by_seq.setdefault(entry.seq, []).append((step, entry))
        return [
            Stage(seq=seq, entries=by_seq[seq], is_finally=is_finally)
            for seq in sorted(by_seq)
        ]


TIMESHEET_HEADER = [
    "run",
    "test_id",
    "test_name",
    "step",
    "status",
    "started",
    "finished",
    "duration_s",
]

# RFC3339 timestamp that `oc logs --timestamps` prepends to each line, e.g.
# "2026-08-31T22:54:42.408123456Z". Matched so it can be stripped back off.
_KUBECTL_TS_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T[\d:.]+Z$")


# ---------------------------------------------------------------------------
# Runner — executes an item's stages in a worker thread, streaming to logs
# ---------------------------------------------------------------------------


class Runner:
    def __init__(self, build: Build):
        self.build = build
        self.run_stamp = ""  # timestamp of the current/last run (set in start)
        self.logs_root = build.build_dir / "logs"
        self.logs_root.mkdir(exist_ok=True)

        self.lock = threading.Lock()
        self.states: list[StepState] = []
        self.current: StepState | None = None  # last active (running) step
        self.result: str = ""  # "", "DONE", "FAILED"
        self.log_dir: Path | None = None
        self.running_item: Item | None = None
        self._thread: threading.Thread | None = None
        self._procs: list[subprocess.Popen] = []
        self._stop = threading.Event()
        self._pod_dir: Path | None = None
        self._followers: list[subprocess.Popen] = []
        self._follower_threads: list[threading.Thread] = []
        self._ts_path = self.logs_root / "timesheet.csv"
        self._ts_lock = threading.Lock()

    @property
    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self, item: Item) -> None:
        if self.is_running:
            return
        stages = self.build.stages_for(item)

        # Timestamp the run folder at the moment of the run so rerunning an item
        # never overwrites an earlier run's logs.
        run_stamp = datetime.now().astimezone().strftime("%Y%m%d_%H%M%S")
        log_dir = self.logs_root / f"{item.item_id}_{_slug(item.name)}_{run_stamp}"
        shell_dir = log_dir / "shell"
        pod_dir = log_dir / "pod"
        shell_dir.mkdir(parents=True, exist_ok=True)
        pod_dir.mkdir(parents=True, exist_ok=True)
        self._pod_dir = pod_dir

        states: list[StepState] = []
        plan: list[tuple[Stage, list[StepState]]] = []
        for stage in stages:
            stage_states: list[StepState] = []
            for step, entry in stage.entries:
                st = StepState(
                    seq=entry.seq,
                    label=entry.path.name,
                    log_path=shell_dir / f"{entry.path.stem}.log",
                    persistent=step.config.get("probe") == "wait-ready",
                )
                states.append(st)
                stage_states.append(st)
            plan.append((stage, stage_states))

        # Create the timesheet with its header up front so it exists the moment
        # a run starts — rows are appended as steps complete (a persistent step's
        # first stage can wait minutes for Ready before the first row lands).
        self._ensure_timesheet()

        with self.lock:
            self.states = states
            self.current = None
            self.result = ""
            self.log_dir = log_dir
            self.running_item = item
            self.run_stamp = run_stamp
            self._stop.clear()

        self._thread = threading.Thread(
            target=self._run_plan, args=(stages, plan), daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        with self.lock:
            for p in self._procs:
                try:
                    p.kill()
                except OSError:
                    pass
            self._procs.clear()
        self._stop_followers()

    # -- worker ------------------------------------------------------------

    def _run_plan(self, stages, plan) -> None:
        # `failed` tracks whether any step failed (drives the final result);
        # `halt` tracks whether a failure's policy requires skipping the rest of
        # the chain. They differ for `onFailure: continue` tests, where a step
        # can fail without halting the remaining normal stages. Finally stages
        # (teardown) always run regardless of both.
        failed = False
        halt = False
        try:
            for stage, stage_states in plan:
                if self._stop.is_set():
                    for st in stage_states:
                        self._set_status(st, "SKIPPED")
                    continue
                if halt and not stage.is_finally:
                    for st in stage_states:
                        self._set_status(st, "SKIPPED")
                    continue

                stage_failed, stage_halt = self._run_stage(stage, stage_states)
                if not stage.is_finally:
                    failed = failed or stage_failed
                    halt = halt or stage_halt
        finally:
            # Persistent-pod followers never exit on their own; stop them once
            # the item's stages are done (teardown has deleted the pods).
            self._stop_followers()

        with self.lock:
            self.result = "FAILED" if failed else "DONE"

    def _run_stage(self, stage: Stage, states: list[StepState]) -> tuple[bool, bool]:
        """Run all entries in a stage concurrently.

        Returns ``(any_failed, halt)``. ``any_failed`` is True if any entry
        exited non-zero. ``halt`` is True if any failing entry's declared
        ``on_failure`` policy is not ``continue`` (i.e. ``skipTest``/``abort``,
        or a lifecycle step with no policy) — meaning the rest of the chain's
        normal stages should be skipped. A ``continue`` failure sets
        ``any_failed`` but not ``halt``, so remaining steps still run.
        """
        procs: list[tuple[Step, ScriptEntry, StepState, subprocess.Popen, object]] = []
        readers: list[threading.Thread] = []

        for (step, entry), st in zip(stage.entries, states):
            st.started = datetime.now().astimezone()
            self._set_status(st, "RUNNING")
            # Handle outlives this scope: it's owned by the streaming thread and
            # closed after proc.wait() below, so a `with` block can't be used.
            log_file = open(st.log_path, "w", encoding="utf-8")  # noqa: SIM115
            proc = subprocess.Popen(
                ["bash", str(entry.path)],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                cwd=str(entry.path.parent),
            )
            with self.lock:
                self._procs.append(proc)
            t = threading.Thread(
                target=self._stream, args=(proc, log_file, st), daemon=True
            )
            t.start()
            readers.append(t)
            procs.append((step, entry, st, proc, log_file))
            # Attach the pod-log follower now, concurrently with the shell
            # step, rather than after it finishes: for a run-to-completion pod
            # the next stage (cleanup) deletes it, so a follower started after
            # the step races the delete and loses. Attaching while the pod is
            # alive lets `oc logs -f` drain to completion first.
            self._maybe_follow_pod(step, st)

        any_failed = False
        halt = False
        for step, entry, st, proc, log_file in procs:
            proc.wait()
            log_file.close()
            with self.lock:
                if proc in self._procs:
                    self._procs.remove(proc)
            st.rc = proc.returncode
            if proc.returncode == 0:
                self._set_status(st, "OK")
            else:
                self._set_status(st, "FAILED")
                any_failed = True
                # Only an explicit `continue` policy keeps the chain going.
                # skipTest/abort (and lifecycle steps, which carry no policy)
                # halt the remaining normal stages.
                if step.on_failure != "continue":
                    halt = True
            self._record_timesheet(st, datetime.now().astimezone())
        for t in readers:
            t.join(timeout=1)
        return any_failed, halt

    def _maybe_follow_pod(self, step: Step, st: StepState) -> None:
        if self._pod_dir is None or self._stop.is_set():
            return
        if step.config.get("command") != "apply":
            return
        pod = step.config.get("pod_name")
        if not pod:
            return
        ns = step.namespace or ""
        log_path = self._pod_dir / f"{pod}.log"
        # The shell step creates the pod asynchronously (its `oc apply` may not
        # have run yet), so the worker waits for the container to start before
        # attaching. Bound the wait by the step's own timeout so a pod that
        # never starts (e.g. a failed apply) doesn't leak a polling thread.
        deadline = time.monotonic() + float(step.config.get("timeout", 600))
        t = threading.Thread(
            target=self._follow_pod, daemon=True, args=(pod, ns, log_path, st, deadline)
        )
        with self.lock:
            st.has_pod = True
            self._follower_threads.append(t)
        t.start()

    def _follow_pod(
        self, pod: str, ns: str, log_path: Path, st: StepState, deadline: float
    ) -> None:
        # Wait for the container to actually start before attaching. Polling for
        # mere existence is not enough: `oc logs -f` errors out ("is waiting to
        # start: ContainerCreating") if it attaches while the pod is still
        # Pending, so wait until the phase moves past Pending (Running, or a
        # terminal phase for a pod that already finished).
        while not self._stop.is_set() and time.monotonic() < deadline:
            if self._pod_phase(pod, ns) in ("Running", "Succeeded", "Failed"):
                break
            time.sleep(0.5)
        else:
            return  # stopped or timed out before the pod started
        # Stream with reconnection. A single `oc logs -f` holds one long-lived
        # connection that this cluster's hosted control plane drops on idle or
        # long-running streams (printing "unexpected EOF" to stderr). When that
        # happens while the pod is still Running, reattach with --since-time so
        # we resume instead of truncating the log. --timestamps supplies that
        # resume point; we strip it back off before writing so the saved log
        # keeps the pod's original output. stderr is discarded so the cosmetic
        # "unexpected EOF" never lands in the file.
        try:
            # Held for the lifetime of the reconnection loop below and closed in
            # its finally, so a `with` block can't wrap it.
            log_file = open(log_path, "w", encoding="utf-8")  # noqa: SIM115
        except OSError:
            return
        last_ts = ""
        try:
            while not self._stop.is_set():
                cmd = ["oc", "logs", "-f", "--timestamps", f"pod/{pod}"]
                if ns:
                    cmd += ["-n", ns]
                if last_ts:
                    cmd.append(f"--since-time={last_ts}")
                try:
                    proc = subprocess.Popen(
                        cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL
                    )
                except OSError:
                    return
                with self.lock:
                    if self._stop.is_set():
                        proc.terminate()
                        return
                    self._followers.append(proc)
                last_ts = self._drain_pod(proc, log_file, st, last_ts)
                with self.lock:
                    if proc in self._followers:
                        self._followers.remove(proc)
                # Reconnect only while the pod is still running; a terminal or
                # vanished pod means the stream ended for good.
                if self._stop.is_set() or self._pod_phase(pod, ns) not in (
                    "Running",
                    "Pending",
                ):
                    break
                time.sleep(1.0)  # brief backoff before reattaching
        finally:
            log_file.close()

    def _pod_phase(self, pod: str, ns: str) -> str:
        cmd = ["oc", "get", "pod", pod, "-o", "jsonpath={.status.phase}"]
        if ns:
            cmd += ["-n", ns]
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, check=False)
        except OSError:
            return ""
        return r.stdout.strip() if r.returncode == 0 else ""

    def _drain_pod(
        self, proc: subprocess.Popen, log_file, st: StepState, last_ts: str = ""
    ) -> str:
        # Read one `oc logs -f --timestamps` stream, strip the kubectl-added
        # RFC3339 timestamp from each line, and write the original content.
        # Returns the latest timestamp seen so a reconnect can resume from it.
        # Lines at or before last_ts are skipped: --since-time is inclusive, so
        # the resume boundary would otherwise be written twice.
        assert proc.stdout is not None
        latest = last_ts
        for raw in iter(proc.stdout.readline, b""):
            text = raw.decode("utf-8", errors="replace")
            ts, sep, rest = text.partition(" ")
            if sep and _KUBECTL_TS_RE.match(ts):
                if last_ts and ts <= last_ts:
                    continue
                latest = max(latest, ts)
                content = rest
            else:
                content = text
            log_file.write(content)
            log_file.flush()  # persist every line before any buffer fills
            with self.lock:
                st.pod_log.append(content.rstrip("\n"))
        try:
            proc.stdout.close()
        except OSError:
            pass
        return latest

    def _ensure_timesheet(self) -> None:
        """Create logs/timesheet.csv with its header if it does not exist yet."""
        with self._ts_lock:
            if self._ts_path.exists():
                return
            with open(self._ts_path, "w", newline="", encoding="utf-8") as f:
                csv.writer(f).writerow(TIMESHEET_HEADER)

    def _record_timesheet(self, st: StepState, finished: datetime) -> None:
        """Append one row per executed step: when it started and finished.

        A persistent step's script exits once its pod is Ready, but the pod
        itself keeps running until teardown — so leave finished/duration empty
        for those rather than logging the script's (misleading) exit time.
        """
        item = self.running_item
        start = st.started
        if st.persistent:
            fin_str, duration = "", ""
        else:
            fin_str = finished.isoformat(sep=" ", timespec="seconds")
            duration = f"{(finished - start).total_seconds():.1f}" if start else ""
        row = [
            self.run_stamp,
            item.item_id if item else "",
            item.name if item else "",
            st.label,
            st.status,
            start.isoformat(sep=" ", timespec="seconds") if start else "",
            fin_str,
            duration,
        ]
        with self._ts_lock:
            new_file = not self._ts_path.exists()
            with open(self._ts_path, "a", newline="", encoding="utf-8") as f:
                w = csv.writer(f)
                if new_file:
                    w.writerow(TIMESHEET_HEADER)
                w.writerow(row)

    def _stop_followers(self) -> None:
        with self.lock:
            procs = list(self._followers)
            self._followers.clear()
        for p in procs:
            try:
                p.terminate()
            except OSError:
                pass
        for t in self._follower_threads:
            t.join(timeout=2)
        self._follower_threads.clear()

    def _stream(self, proc: subprocess.Popen, log_file, st: StepState) -> None:
        assert proc.stdout is not None
        for raw in iter(proc.stdout.readline, b""):
            text = raw.decode("utf-8", errors="replace")
            log_file.write(text)
            log_file.flush()  # persist every line before any buffer fills
            with self.lock:
                st.shell_log.append(text.rstrip("\n"))
        try:
            proc.stdout.close()
        except OSError:
            pass

    def _set_status(self, st: StepState, status: str) -> None:
        with self.lock:
            st.status = status
            if status == "RUNNING":
                self.current = st  # log view follows the last active step


def _slug(name: str) -> str:
    return re.sub(r"[^a-zA-Z0-9._-]+", "-", name).strip("-")


# ---------------------------------------------------------------------------
# Curses UI
# ---------------------------------------------------------------------------

# Color pairs (green theme):
#   1 red   2 green   3 yellow   4 green(dim accent)   5 green(headers)
#   6 black-on-green (bars + selection)
STATUS_COLOR = {
    "PENDING": 0,
    "RUNNING": 3,  # yellow
    "OK": 2,  # green
    "FAILED": 1,  # red
    "SKIPPED": 4,  # dim green
}


class Dashboard:
    def __init__(self, build: Build, runner: Runner):
        self.build = build
        self.runner = runner
        self.sel = 0

    def run(self, stdscr) -> None:
        curses.curs_set(0)
        stdscr.nodelay(True)
        stdscr.timeout(150)
        self._init_colors()

        while True:
            self._draw(stdscr)
            try:
                ch = stdscr.getch()
            except KeyboardInterrupt:
                ch = ord("q")

            if ch == -1:
                continue
            if ch in (curses.KEY_UP, ord("k")):
                self.sel = max(0, self.sel - 1)
            elif ch in (curses.KEY_DOWN, ord("j")):
                self.sel = min(len(self.build.items) - 1, self.sel + 1)
            elif ch in (curses.KEY_ENTER, 10, 13, ord("r")):
                if not self.runner.is_running:
                    self.runner.start(self.build.items[self.sel])
            elif ch in (ord("q"), ord("Q")):
                if self.runner.is_running:
                    self.runner.stop()
                    if self.runner._thread:
                        self.runner._thread.join(timeout=5)
                break

    def _init_colors(self) -> None:
        if not curses.has_colors():
            return
        curses.start_color()
        curses.use_default_colors()
        g = curses.COLOR_GREEN
        curses.init_pair(1, curses.COLOR_RED, -1)  # failed
        curses.init_pair(2, g, -1)  # ok / primary
        curses.init_pair(3, curses.COLOR_YELLOW, -1)  # running
        curses.init_pair(4, g, -1)  # accent (used dim)
        curses.init_pair(5, g, -1)  # titles / borders
        curses.init_pair(6, curses.COLOR_BLACK, g)  # selection bar

    # -- drawing -----------------------------------------------------------

    def _draw(self, stdscr) -> None:
        stdscr.erase()
        h, w = stdscr.getmaxyx()
        self._scr, self._h, self._w = stdscr, h, w
        if h < 8 or w < 54:
            self._t(0, 0, "Terminal too small (need 54x8).")
            stdscr.refresh()
            return

        self._draw_header()

        pane_y, pane_h = 1, h - 2
        left_w = min(34, max(24, w // 3))
        # Left pane: item list.
        self._box(pane_y, 0, pane_h, left_w, "Tests & Lifecycle")
        self._draw_left(pane_y + 1, 2, pane_h - 2, left_w - 4)
        # Right pane: active item detail.
        run_item = self.runner.running_item
        rtitle = f"{run_item.item_id} · {run_item.name}" if run_item else "Details"
        self._box(pane_y, left_w, pane_h, w - left_w, rtitle)
        self._draw_right(pane_y + 1, left_w + 2, pane_h - 2, w - left_w - 4)

        self._draw_footer(h - 1)
        stdscr.refresh()

    def _draw_header(self) -> None:
        run = self.runner
        self._t(0, 1, "⬢ UAT Manual Runner", self._c(2) | curses.A_BOLD)
        meta = f"   build {self.build.build_dir.name}"
        if run.run_stamp:
            meta += f" · run {run.run_stamp}"
        self._t(0, 21, meta, curses.A_DIM)
        # Right-aligned run status with a colored dot.
        if run.is_running:
            label, pair = f"RUNNING · {run.running_item.item_id}", 3
        elif run.result and run.running_item:
            pair = 2 if run.result == "DONE" else 1
            label = f"{run.result} · {run.running_item.item_id}"
        else:
            label, pair = "idle", 4
        seg = f"● {label}"
        self._t(
            0,
            self._w - len(seg) - 2,
            seg,
            self._c(pair) | (curses.A_BOLD if pair != 4 else curses.A_DIM),
        )

    def _item_label(self, item: Item) -> str:
        if item.kind == "lifecycle":
            return item.name
        return f"{item.item_id} {item.name}"

    def _draw_left(self, y0, x0, height, width) -> None:
        run = self.runner
        row, last_kind = y0, None
        for i, item in enumerate(self.build.items):
            if row >= y0 + height:
                break
            # Section rule when the kind changes.
            if item.kind != last_kind:
                label = "LIFECYCLE" if item.kind == "lifecycle" else "TESTS"
                rule = f"{label} " + "─" * max(0, width - len(label) - 1)
                self._t(row, x0, rule[:width], self._c(4) | curses.A_DIM)
                last_kind = item.kind
                row += 1
                if row >= y0 + height:
                    break

            is_sel = i == self.sel
            dot, dpair = self._item_dot(item, run)
            prefix = "▸" if is_sel else dot
            line = f"{prefix} {self._item_label(item)}"
            if is_sel:
                self._t(row, x0, line.ljust(width), self._c(6) | curses.A_BOLD)
            else:
                self._t(row, x0, line.ljust(width))
                if dot != " ":
                    self._t(row, x0, dot, self._c(dpair) | curses.A_BOLD)
            row += 1

    def _item_dot(self, item: Item, run: Runner) -> tuple[str, int]:
        """Small status glyph for an item in the list."""
        if item is not run.running_item:
            return " ", 0
        if run.is_running:
            return "●", 3
        if run.result == "DONE":
            return "●", 2
        if run.result == "FAILED":
            return "●", 1
        return "•", 4

    def _draw_right(self, y0, x0, height, width) -> None:
        with self.runner.lock:
            states = list(self.runner.states)
            current = self.runner.current
            cur_label = current.label if current else None
            cur_has_pod = current.has_pod if current else False
            shell_lines = list(current.shell_log) if current else []
            pod_lines = list(current.pod_log) if current else []

        if current is None and not states:
            self._t(
                y0 + 1,
                x0,
                "Select an item and press Enter to run.",
                self._c(4) | curses.A_DIM,
            )
            return

        # -- steps with right-aligned status badges --------------------------
        self._t(y0, x0, "STEPS", self._c(5) | curses.A_BOLD)
        row = y0 + 1
        status_h = min(len(states), max(3, (height - 5) // 2))
        if len(states) > status_h:
            self._t(
                row,
                x0,
                f"… {len(states) - status_h} more above",
                self._c(4) | curses.A_DIM,
            )
            row += 1
        for st in states[-status_h:] if len(states) > status_h else states:
            if row >= y0 + height:
                break
            is_cur = st is current
            marker = "▶ " if is_cur else "  "
            badge, bpair, battr = self._badge(st.status)
            name_w = max(1, width - 8)
            self._t(
                row,
                x0,
                f"{marker}{st.label}"[:name_w].ljust(name_w),
                curses.A_BOLD if is_cur else 0,
            )
            self._t(row, x0 + name_w, badge.rjust(8), self._c(bpair) | battr)
            row += 1

        # -- divider ---------------------------------------------------------
        self._t(row, x0, "─" * width, self._c(5) | curses.A_DIM)
        row += 1
        log_h = (y0 + height) - row
        if log_h <= 0 or current is None:
            return

        # -- active step logs: shell (+ pod when applicable) -----------------
        if cur_has_pod:
            shell_h = log_h // 2
            self._draw_logblock(
                row, x0, shell_h, width, f"shell · {cur_label}", shell_lines
            )
            self._draw_logblock(
                row + shell_h, x0, log_h - shell_h, width, "pod", pod_lines
            )
        else:
            self._draw_logblock(
                row, x0, log_h, width, f"shell · {cur_label}", shell_lines
            )

    def _draw_logblock(self, y0, x0, h, width, title, lines) -> None:
        if h <= 0:
            return
        self._t(y0, x0, title[:width], self._c(4) | curses.A_DIM | curses.A_BOLD)
        body_h = h - 1
        if body_h <= 0:
            return
        for i, line in enumerate(lines[-body_h:]):
            self._t(y0 + 1 + i, x0, line[:width])

    def _badge(self, status: str) -> tuple[str, int, int]:
        return {
            "PENDING": ("–", 4, curses.A_DIM),
            "RUNNING": ("● run", 3, curses.A_BOLD),
            "OK": ("✔ ok", 2, curses.A_BOLD),
            "FAILED": ("✘ fail", 1, curses.A_BOLD),
            "SKIPPED": ("skip", 4, curses.A_DIM),
        }.get(status, (status.lower(), 0, 0))

    def _draw_footer(self, y: int) -> None:
        self._t(y, 1, "↑/↓ move", self._c(2) | curses.A_BOLD)
        self._t(y, 11, "· ⏎ run  · r rerun  · q quit", curses.A_DIM)

    # -- helpers -----------------------------------------------------------

    def _box(self, y, x, h, wd, title="") -> None:
        c = self._c(5) | curses.A_DIM
        self._t(y, x, "╭" + "─" * (wd - 2) + "╮", c)
        self._t(y + h - 1, x, "╰" + "─" * (wd - 2) + "╯", c)
        for i in range(1, h - 1):
            self._t(y + i, x, "│", c)
            self._t(y + i, x + wd - 1, "│", c)
        if title:
            self._t(y, x + 2, f" {title} ", self._c(5) | curses.A_BOLD)

    def _t(self, y, x, text, attr=0) -> None:
        self._addstr(self._scr, y, x, text, self._h, self._w, attr)

    def _c(self, pair: int) -> int:
        return curses.color_pair(pair) if curses.has_colors() else 0

    @staticmethod
    def _addstr(win, y, x, text, max_y, max_x, attr=0) -> None:
        if y >= max_y or x >= max_x:
            return
        text = text[: max_x - x]
        try:
            win.addstr(y, x, text, attr)
        except curses.error:
            pass  # last cell / resize races


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Curses dashboard for running UAT test suites manually."
    )
    parser.add_argument("build_dir", help="build directory (steps.json + manual/)")
    args = parser.parse_args()

    build = Build(Path(args.build_dir))
    runner = Runner(build)
    dashboard = Dashboard(build, runner)

    try:
        curses.wrapper(dashboard.run)
    finally:
        if runner.is_running:
            runner.stop()


if __name__ == "__main__":
    main()
