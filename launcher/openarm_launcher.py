#!/usr/bin/env python3
# Copyright 2026 Enactic, Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Desktop launcher (supervisor) for OpenArm data collection dataflows.

The operator only clicks a desktop shortcut. This process:

1. serves a Japanese status page (default http://127.0.0.1:8080/),
2. configures the CAN interfaces (needs a passwordless sudo rule),
3. runs `uv run dora build ... --uv` when needed, then `uv run dora run ... --uv`,
4. redirects the browser to the task screen once the UI node is up,
5. shows any failure as red text on that page instead of in a terminal,
   and forwards error lines to the task screen while the dataflow runs.

Standard library only, so it can run with the system Python.
"""

import argparse
import collections
import datetime
import html
import json
import os
import pathlib
import re
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs



def show_desktop_message(message, kind="info"):
    """Show a dialog for a shortcut that has no terminal to print to."""
    sys.stdout.write(message + "\n")
    title = "OpenArm データ収集"
    for argv in (
        ["zenity", f"--{kind}", "--width=520", "--title", title, "--text", message],
        ["kdialog", "--title", title, f"--{'sorry' if kind == 'error' else 'msgbox'}",
         message],
        ["notify-send", title, message],
    ):
        if shutil.which(argv[0]):
            subprocess.Popen(argv, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return True
    return False


def show_desktop_error(message):
    """Report a failure that happens before the web page exists.

    A desktop shortcut has no terminal, so a bare traceback would look like
    "nothing happened" to the operator. Show a dialog and keep a crash log.
    """
    sys.stderr.write(message + "\n")
    try:
        base = os.getenv("XDG_STATE_HOME") or os.path.expanduser("~/.local/state")
        path = pathlib.Path(base) / "openarm-launcher"
        path.mkdir(parents=True, exist_ok=True)
        stamp = datetime.datetime.now().isoformat(timespec="seconds")
        with (path / "launcher-crash.log").open("a", encoding="utf-8") as log:
            log.write(f"[{stamp}] {message}\n")
    except OSError:
        pass
    return show_desktop_message(message, kind="error")


try:
    import yaml
except ModuleNotFoundError:  # pragma: no cover - configuration error
    show_desktop_error(
        "PyYAML が見つかりません。`sudo apt install python3-yaml` "
        "または `pip install pyyaml` を実行してください。"
    )
    raise

LOG_LINES = 4000
ERROR_LINES = 40
UI_WAIT_TIMEOUT_S = 180.0

# Lines that mean "something went wrong" in dora / node output. Kept
# deliberately narrow: a false positive paints the task screen red.
ERROR_PATTERNS = (
    re.compile(r"Traceback \(most recent call last\)"),
    re.compile(r"^\s*(ERROR|Error:|error:)"),
    re.compile(r"^\s*\w*(Error|Exception):"),
    re.compile(r"\b(Failed to|failed to|failed with)\b"),
    re.compile(r"No such file or directory"),
    re.compile(r"Permission denied"),
    re.compile(r"Connection refused"),
    re.compile(r"could not be found|not found in PATH"),
)

# Noise that matches the patterns above but is not an operator-visible failure.
ERROR_IGNORE_PATTERNS = (
    re.compile(r"error_pattern|ERROR_PATTERNS"),
    re.compile(r"log_level"),
)


def _read_cmdline(pid):
    try:
        raw = pathlib.Path(f"/proc/{pid}/cmdline").read_bytes()
    except OSError:
        return []
    return [part for part in raw.decode("utf-8", "replace").split("\0") if part]


def dataflow_process_name(argv):
    """Return the dora program name a command line runs, or None.

    Matching is on program names, never on the whole command line: the
    repository path itself contains "dora-openarm", so a substring match would
    also hit the launcher and any shell sitting in the directory.
    """
    if not argv:
        return None
    names = [pathlib.PurePath(argv[0]).name]
    if names[0] in ("uv", "uvx") and len(argv) > 2 and argv[1] == "run":
        names.append(pathlib.PurePath(argv[2]).name)
    elif names[0].startswith("python") and len(argv) > 1:
        names.append(pathlib.PurePath(argv[1]).name)
    for name in names:
        if name == "dora" or name.startswith(("dora-", "opencv-video-capture")):
            return name
    return None


def stale_dataflow_pids(repo_dir, exclude=()):
    """Return pids of dora processes that were started for this repository."""
    repo = str(pathlib.Path(repo_dir).resolve())
    found = []
    for entry in pathlib.Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        pid = int(entry.name)
        if pid == os.getpid() or pid in exclude:
            continue
        name = dataflow_process_name(_read_cmdline(pid))
        if not name:
            continue
        try:
            cwd = os.readlink(f"/proc/{pid}/cwd")
        except OSError:
            continue  # another user's process, or it just exited
        if cwd == repo or cwd.startswith(repo + os.sep):
            found.append(pid)
    return found


def kill_pids(pids, timeout_s=5.0):
    """SIGTERM then SIGKILL the given pids. Returns the pids that were signalled."""
    signalled = []
    for pid in pids:
        try:
            os.kill(pid, signal.SIGTERM)
            signalled.append(pid)
        except OSError:
            pass
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        alive = [pid for pid in signalled if pathlib.Path(f"/proc/{pid}").exists()]
        if not alive:
            return signalled
        time.sleep(0.2)
    for pid in signalled:
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            pass
    return signalled


def _now():
    return datetime.datetime.now().strftime("%H:%M:%S")


def state_dir():
    """Return the directory for logs and build stamps."""
    base = os.getenv("XDG_STATE_HOME") or os.path.expanduser("~/.local/state")
    path = pathlib.Path(base) / "openarm-launcher"
    path.mkdir(parents=True, exist_ok=True)
    return path


class Config:
    """launcher.yaml plus the paths derived from it."""

    def __init__(self, path):
        self.path = pathlib.Path(path).resolve()
        raw = yaml.safe_load(self.path.read_text(encoding="utf-8")) or {}
        # launcher.yaml lives in <repo>/launcher/, dataflows in <repo>/.
        self.repo_dir = (
            pathlib.Path(raw["repo_dir"]).expanduser().resolve()
            if raw.get("repo_dir")
            else self.path.parent.parent
        )
        self.port = int(raw.get("port", 8080))
        self.ui_url = raw.get("ui_url", "http://127.0.0.1:8000/")
        self.venv = raw.get("venv")
        self.uv = raw.get("uv")
        self.cleanup_before_start = bool(raw.get("cleanup_before_start", True))
        self.dataset = raw.get("dataset") or {}
        self.can_setup = raw.get("can_setup") or {}
        self.entries = raw.get("entries") or []
        if not self.entries:
            raise ValueError(f"{self.path} に entries がありません")

    def entry(self, entry_id):
        """Return the entry with the given id, or None."""
        for entry in self.entries:
            if entry.get("id") == entry_id:
                return entry
        return None

    @property
    def ui_host_port(self):
        """Return (host, port) of the task screen."""
        parsed = urlparse(self.ui_url)
        return parsed.hostname or "127.0.0.1", parsed.port or 80

    def uv_command(self):
        """Return the uv executable to use."""
        for candidate in self._uv_candidates():
            if candidate.exists():
                return str(candidate)
        return shutil.which("uv")

    def _uv_candidates(self):
        if self.uv:
            yield pathlib.Path(self.uv).expanduser()
        if self.venv:
            yield pathlib.Path(self.venv).expanduser() / "bin" / "uv"
        yield self.repo_dir / ".venv" / "bin" / "uv"
        yield pathlib.Path.home() / ".local" / "bin" / "uv"

    def dora_argv(self, uv, *args):
        """Return the argv that runs dora through uv, e.g. `uv run dora run x --uv`."""
        return [uv, "run", "dora", *args, "--uv"]


class Runner:
    """Runs one entry: CAN setup, dora build, dora run, and supervision."""

    def __init__(self, config):
        self.config = config
        self.lock = threading.Lock()
        self.state = "idle"  # idle | preparing | running | stopped | error
        self.step = ""
        self.hint = None
        self.entry = None
        self.errors = []  # [{"time", "text"}]
        self.log = collections.deque(maxlen=LOG_LINES)
        self.log_path = None
        self.proc = None
        self.started_at = None
        self._log_file = None
        self._seen_errors = collections.deque(maxlen=100)
        self._in_traceback = False
        self._traceback = []

    # -- state helpers -------------------------------------------------

    def snapshot(self):
        """Return the state as a JSON-serializable dict."""
        with self.lock:
            return {
                "state": self.state,
                "step": self.step,
                "hint": self.hint,
                "entry": self.entry,
                "errors": list(self.errors[-ERROR_LINES:]),
                "log_tail": list(self.log)[-ERROR_LINES:],
                "ui_url": self.config.ui_url,
                "log_path": str(self.log_path) if self.log_path else None,
                "started_at": self.started_at,
            }

    def _set(self, state=None, step=None):
        with self.lock:
            if state is not None:
                self.state = state
            if step is not None:
                self.step = step

    def _add_error(self, text):
        text = text.strip()
        if not text:
            return
        with self.lock:
            if text in self._seen_errors:
                return
            self._seen_errors.append(text)
            self.errors.append({"time": _now(), "text": text})
            self.errors = self.errors[-200:]
        self._forward_error_to_ui(text)

    def _forward_error_to_ui(self, text):
        """Push an error line to the task screen so it shows up in red there."""
        if self.state != "running":
            return

        def send():
            url = self.config.ui_url.rstrip("/") + "/api/error"
            payload = json.dumps({"source": "dataflow", "message": text})
            request = urllib.request.Request(
                url,
                data=payload.encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            try:
                urllib.request.urlopen(request, timeout=1.0).close()
            except (urllib.error.URLError, OSError, TimeoutError):
                pass  # The task screen may be gone; the launcher page still shows it.

        threading.Thread(target=send, daemon=True).start()

    # -- logging -------------------------------------------------------

    def _open_log(self, entry_id):
        stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
        self.log_path = state_dir() / "logs" / f"{entry_id}-{stamp}.log"
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        self._log_file = self.log_path.open("w", encoding="utf-8", errors="replace")

    def _write(self, line):
        line = line.rstrip("\n")
        with self.lock:
            self.log.append(line)
        if self._log_file:
            self._log_file.write(line + "\n")
            self._log_file.flush()

    def _scan_for_errors(self, line):
        """Record error-looking output lines, collapsing tracebacks into one entry."""
        if any(pattern.search(line) for pattern in ERROR_IGNORE_PATTERNS):
            return
        if self._in_traceback:
            self._traceback.append(line)
            # The exception line ends a traceback: not indented, "Name: message".
            if line and not line[0].isspace():
                self._add_error(line.strip())
                self._in_traceback = False
                self._traceback = []
            elif len(self._traceback) > 60:
                self._in_traceback = False
                self._traceback = []
            return
        if ERROR_PATTERNS[0].search(line):
            self._in_traceback = True
            self._traceback = [line]
            return
        for pattern in ERROR_PATTERNS[1:]:
            if pattern.search(line):
                self._add_error(line.strip())
                return

    # -- steps ---------------------------------------------------------

    def _run_step(self, argv, cwd=None, env=None):
        """Run a blocking sub-command, streaming its output into the log."""
        self._write(f"$ {' '.join(argv)}")
        try:
            proc = subprocess.Popen(
                argv,
                cwd=cwd or str(self.config.repo_dir),
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
            )
        except FileNotFoundError as error:
            self._add_error(str(error))
            return 127
        for line in proc.stdout:
            self._write(line)
            self._scan_for_errors(line)
        return proc.wait()

    def _can_interfaces_missing(self):
        """Return the configured CAN interfaces that the kernel does not know."""
        missing = []
        for name in self.config.can_setup.get("interfaces", []):
            if not pathlib.Path(f"/sys/class/net/{name}").exists():
                missing.append(name)
        return missing

    def _setup_can(self):
        command = self.config.can_setup.get("command")
        if not command:
            return True
        self._set(step="CAN インタフェースを設定しています…")
        missing = self._can_interfaces_missing()
        if missing:
            self._fail(
                "CAN インタフェースが見つかりません: " + ", ".join(missing),
                hint=(
                    "USB-CAN アダプタがすべて PC に挿さっているか確認してください。"
                    "挿し直した場合は数秒待ってから「再試行」を押してください。"
                ),
            )
            return False
        code = self._run_step(list(command))
        if code != 0:
            if code == 1 and any(
                "password" in e["text"].lower() or "sudo" in e["text"].lower()
                for e in self.errors[-5:]
            ):
                hint = (
                    "sudo のパスワードなし実行が設定されていません。"
                    "管理者が launcher/install.sh を一度実行してください。"
                )
            else:
                hint = (
                    "CAN の設定に失敗しました。USB-CAN アダプタを挿し直し、"
                    "「再試行」を押してください。改善しない場合は担当者に連絡してください。"
                )
            self._fail(f"CAN インタフェースの設定に失敗しました (終了コード {code})", hint)
            return False
        return True

    def _build_stamp(self, entry, dataflow_path):
        stamp = state_dir() / f"build-{entry['id']}.stamp"
        current = f"{dataflow_path.stat().st_mtime_ns}"
        return stamp, current

    def _build(self, entry, dataflow_path, uv):
        if not entry.get("build", True):
            return True
        stamp, current = self._build_stamp(entry, dataflow_path)
        if stamp.exists() and stamp.read_text(encoding="utf-8").strip() == current:
            return True
        self._set(step="ノードを準備しています…（初回は数分かかります）")
        code = self._run_step(self.config.dora_argv(uv, "build", str(dataflow_path)))
        if code != 0:
            recent = list(self.log)[-400:]
            if any("externally-managed-environment" in line for line in recent):
                # dora fell back to the system pip: the run did not go through uv.
                hint = (
                    "システムの pip が使われています。ランチャーが古い可能性があるため、"
                    "リポジトリを最新版に更新し、ランチャーを再起動してください。"
                )
            else:
                hint = "ネットワーク接続を確認して「再試行」を押してください。"
            self._fail(f"ノードの準備 (dora build) に失敗しました (終了コード {code})", hint)
            return False
        stamp.write_text(current, encoding="utf-8")
        return True

    def _dataset_target(self, entry):
        """Return (directory, name) for the recorder: <root>/<date>/<datetime>."""
        # entry の指定 > 環境変数 DATASET_ROOT > launcher.yaml
        root = entry.get(
            "dataset_root",
            os.getenv("DATASET_ROOT") or self.config.dataset.get("root"),
        )
        if not root:
            return None
        path = pathlib.Path(str(root)).expanduser()
        if not path.is_absolute():
            path = self.config.repo_dir / path
        now = datetime.datetime.now()
        date = now.strftime(self.config.dataset.get("date_format", "%Y-%m-%d"))
        session = now.strftime(
            self.config.dataset.get("session_format", "%Y-%m-%d_%H-%M-%S")
        )
        return path / date, session

    def _prepare_dataset_directory(self, directory):
        """Create today's dataset directory, or say on screen why we cannot."""
        try:
            directory.mkdir(parents=True, exist_ok=True)
            probe = directory / ".launcher-write-test"
            probe.touch()
            probe.unlink()
        except OSError as error:
            self._fail(
                f"データの保存先を用意できません: {directory}",
                hint=(
                    f"{error.strerror}。保存先のディスクがマウントされているか"
                    "確認してください。改善しない場合は担当者に連絡してください。"
                ),
            )
            return False
        return True

    def _prepare_dataflow(self, entry):
        """Return the dataflow to run, with metadata and dataset path applied."""
        dataflow_path = self.config.repo_dir / entry["dataflow"]
        if not dataflow_path.exists():
            self._fail(
                f"データフロー定義が見つかりません: {dataflow_path}",
                hint="launcher/launcher.yaml の dataflow を確認してください。",
            )
            return None
        metadata = entry.get("metadata")
        if metadata and not (self.config.repo_dir / metadata).exists():
            self._fail(
                f"メタデータが見つかりません: {self.config.repo_dir / metadata}",
                hint="launcher/launcher.yaml の metadata を確認してください。",
            )
            return None
        dataset = self._dataset_target(entry)
        if dataset and not self._prepare_dataset_directory(dataset[0]):
            return None

        dataflow = yaml.safe_load(dataflow_path.read_text(encoding="utf-8"))
        notes = []
        for node in dataflow.get("nodes", []):
            env = node.get("env") or {}
            if metadata and "METADATA_FILE" in env and env["METADATA_FILE"] != metadata:
                env["METADATA_FILE"] = metadata
                node["env"] = env
                notes.append(f"metadata を {metadata} に差し替え")
            if dataset and "dataset-recorder" in str(node.get("path", "")):
                # <root>/<date>/<datetime>/ so every session keeps its own folder.
                env["DIRECTORY"] = str(dataset[0])
                env["NAME"] = dataset[1]
                node["env"] = env
                notes.append(f"保存先を {dataset[0] / dataset[1]} に設定")
        if not notes:
            return dataflow_path
        generated = self.config.repo_dir / f".launcher-{entry['id']}.yaml"
        generated.write_text(
            yaml.safe_dump(dataflow, sort_keys=False, allow_unicode=True),
            encoding="utf-8",
        )
        for note in dict.fromkeys(notes):
            self._write(f"# {note}（{generated.name}）")
        return generated

    def _fail(self, message, hint=None):
        with self.lock:
            self.state = "error"
            self.step = message
            self.hint = hint
        self._add_error(message)

    # -- lifecycle -----------------------------------------------------

    def start(self, entry_id):
        """Start an entry. Returns (ok, message)."""
        with self.lock:
            if self.state in ("preparing", "running"):
                return False, "すでに起動しています"
            entry = self.config.entry(entry_id)
            if entry is None:
                return False, f"不明な entry です: {entry_id}"
            self.entry = entry
            self.state = "preparing"
            self.step = "起動を準備しています…"
            self.errors = []
            self._seen_errors.clear()
            self.log.clear()
            self.hint = None
            self.started_at = _now()
        self._open_log(entry_id)
        threading.Thread(target=self._run, args=(entry,), daemon=True).start()
        return True, "起動しました"

    def _run(self, entry):
        uv = self.config.uv_command()
        if uv is None:
            self._fail(
                "uv コマンドが見つかりません",
                hint="launcher/launcher.yaml の uv に uv の絶対パスを指定してください。",
            )
            return
        if self.config.cleanup_before_start:
            self._set(step="前回のプロセスを片付けています…")
            self.cleanup_stale("起動前")
        if entry.get("can_setup", True) and not self._setup_can():
            return
        dataflow_path = self._prepare_dataflow(entry)
        if dataflow_path is None:
            return
        if not self._build(entry, dataflow_path, uv):
            return

        if not self._wait_for_free_ui_port():
            return

        self._set(step="データフローを起動しています…")
        env = dict(os.environ)
        env.setdefault("PYTHONUNBUFFERED", "1")
        try:
            self.proc = subprocess.Popen(
                self.config.dora_argv(uv, "run", str(dataflow_path)),
                cwd=str(self.config.repo_dir),
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
                start_new_session=True,
            )
        except OSError as error:
            self._fail(f"dora run を開始できませんでした: {error}")
            return

        threading.Thread(target=self._pump_output, daemon=True).start()
        if not self._wait_for_ui():
            return
        self._set(state="running", step="実行中")
        self._monitor()

    def force_stop_all(self):
        """Stop everything and leave a state the operator can act on.

        Pressing this in an error state must clear that state: otherwise the
        page keeps rendering the same red screen and looks broken.
        """
        self.stop()
        count = self.cleanup_stale("手動")
        with self.lock:
            self.state = "stopped"
            self.step = (
                f"すべて停止しました（{count} 件のプロセスを終了）"
                if count
                else "すべて停止しました"
            )
            self.errors = []
            self._seen_errors.clear()
            self.hint = None
        return count

    def cleanup_stale(self, reason):
        """Kill dora processes left over from an earlier run. Returns the count."""
        exclude = ()
        if self.proc is not None and self.proc.poll() is None:
            exclude = (self.proc.pid,)
        pids = stale_dataflow_pids(self.config.repo_dir, exclude=exclude)
        if not pids:
            return 0
        self._write(f"# 残っているプロセスを停止します（{reason}）: {pids}")
        kill_pids(pids)
        return len(pids)

    def _wait_for_free_ui_port(self):
        """Make sure no leftover task screen owns the UI port before starting.

        A previous run that did not die completely would otherwise make
        `_wait_for_ui` succeed at once and hand the operator a dead screen.
        """
        host, port = self.config.ui_host_port
        for _ in range(20):  # a normal shutdown releases the port in a second
            if not port_in_use(port):
                return True
            time.sleep(0.5)
        self._fail(
            f"前回の収集プロセスが残っています（ポート {port} が使用中）",
            hint=(
                "少し待ってから「再試行」を押してください。"
                "改善しない場合は PC を再起動するか担当者に連絡してください。"
            ),
        )
        return False

    def _pump_output(self):
        for line in self.proc.stdout:
            self._write(line)
            self._scan_for_errors(line)

    def _wait_for_ui(self):
        """Block until the task screen accepts connections, or the run dies."""
        self._set(step="タスク画面の準備を待っています…")
        host, port = self.config.ui_host_port
        deadline = time.monotonic() + UI_WAIT_TIMEOUT_S
        while time.monotonic() < deadline:
            if self.proc.poll() is not None:
                self._fail(
                    f"起動に失敗しました (終了コード {self.proc.returncode})",
                    hint="下のエラー内容を担当者に伝えてください。「再試行」で再起動できます。",
                )
                return False
            try:
                with socket.create_connection((host, port), timeout=0.5):
                    return True
            except OSError:
                time.sleep(0.5)
        self._fail(
            "タスク画面が時間内に起動しませんでした",
            hint="「再試行」を押してください。改善しない場合は担当者に連絡してください。",
        )
        self.stop()
        return False

    def _monitor(self):
        code = self.proc.wait()
        # SIGINT (Ctrl-C equivalent) and a UI "終了" both count as a normal end.
        if code in (0, -signal.SIGINT, -signal.SIGTERM, 130):
            self._set(state="stopped", step="終了しました")
        else:
            self._fail(
                f"データフローが異常終了しました (終了コード {code})",
                hint="下のエラー内容を担当者に伝えてください。「再試行」で再起動できます。",
            )

    def _session_pids(self, sid):
        """Return the pids that belong to session `sid` (the dataflow's own)."""
        pids = []
        for entry in pathlib.Path("/proc").iterdir():
            if not entry.name.isdigit():
                continue
            try:
                stat = (entry / "stat").read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue  # the process is already gone
            try:
                # "... (comm) state ppid pgrp session ..."; comm may contain spaces.
                fields = stat[stat.rindex(")") + 2 :].split()
                if int(fields[3]) == sid:
                    pids.append(int(entry.name))
            except (ValueError, IndexError):
                continue
        return pids

    def _kill_session_leftovers(self, sid):
        """Kill nodes that escaped the process group by making their own.

        `dora run` gets its own session, so everything it spawned is in that
        session even when it is in another process group. Without this, node
        processes survive a stop and pile up run after run.
        """
        if sid is None or sid == os.getsid(0):
            return
        for sig in (signal.SIGTERM, signal.SIGKILL):
            pids = [pid for pid in self._session_pids(sid) if pid != os.getpid()]
            if not pids:
                return
            for pid in pids:
                try:
                    os.kill(pid, sig)
                except OSError:
                    pass
            for _ in range(20):
                if not self._session_pids(sid):
                    return
                time.sleep(0.1)

    def stop(self):
        """Stop the running dataflow the way Ctrl-C would."""
        proc = self.proc
        # A failure already explained on screen must survive the cleanup.
        keep_error = self.state == "error"
        # `dora run` is started with start_new_session=True, so its pid is the
        # session id of every process it spawned (valid even after it exited).
        sid = proc.pid if proc is not None else None
        if proc is None or proc.poll() is not None:
            self._kill_session_leftovers(sid)
            if not keep_error:
                self._set(state="stopped", step="終了しました")
            return
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGINT)
        except OSError:
            pass
        for _ in range(100):
            if proc.poll() is not None:
                break
            time.sleep(0.1)
        for sig in (signal.SIGTERM, signal.SIGKILL):
            if proc.poll() is not None:
                break
            try:
                os.killpg(os.getpgid(proc.pid), sig)
            except OSError:
                pass
            for _ in range(30):
                if proc.poll() is not None:
                    break
                time.sleep(0.1)
        self._kill_session_leftovers(sid)
        if not keep_error:
            self._set(state="stopped", step="終了しました")


# ----------------------------------------------------------------------
# Web UI
# ----------------------------------------------------------------------

PAGE_CSS = """
* { box-sizing: border-box; margin: 0; padding: 0; }
body {
  font-family: "Noto Sans JP", "Noto Sans CJK JP", sans-serif;
  background: #0a0a0f; color: #e0e0e0; min-height: 100vh;
  display: flex; flex-direction: column; align-items: center;
  padding: 48px 24px; gap: 28px;
}
h1 { font-size: 30px; color: #00e5ff; letter-spacing: 0.05em; }
.card {
  background: #1a1a2e; border-radius: 16px; padding: 28px 36px;
  width: min(900px, 100%); display: flex; flex-direction: column; gap: 16px;
}
.step { font-size: 26px; font-weight: bold; }
.sub { font-size: 17px; color: #9a9ab0; line-height: 1.7; }
.spinner {
  width: 36px; height: 36px; border-radius: 50%;
  border: 5px solid rgba(0, 229, 255, 0.2); border-top-color: #00e5ff;
  animation: spin 1s linear infinite;
}
@keyframes spin { to { transform: rotate(360deg); } }
.error-card { background: #2a0f16; border: 3px solid #ff3355; }
.error-title { font-size: 30px; font-weight: bold; color: #ff3355; }
.error-hint { font-size: 19px; color: #ffd7de; line-height: 1.7; }
.error-list {
  list-style: none; display: flex; flex-direction: column; gap: 8px;
  max-height: 320px; overflow-y: auto;
}
.error-list li {
  font-family: ui-monospace, monospace; font-size: 15px; color: #ff8fa3;
  background: rgba(255, 51, 85, 0.08); border-left: 4px solid #ff3355;
  padding: 8px 12px; white-space: pre-wrap; word-break: break-all;
}
.buttons { display: flex; gap: 16px; flex-wrap: wrap; }
button, .button {
  font-family: inherit; font-size: 20px; font-weight: bold;
  padding: 16px 32px; border-radius: 12px; cursor: pointer;
  border: 2px solid rgba(0, 255, 136, 0.4); background: rgba(0, 255, 136, 0.1);
  color: #00ff88; text-decoration: none; display: inline-block;
}
button:hover, .button:hover { background: rgba(0, 255, 136, 0.2); }
button.secondary, .button.secondary {
  border-color: rgba(136, 136, 136, 0.4); background: rgba(136, 136, 136, 0.1);
  color: #bbb;
}
.entry-list { display: flex; flex-direction: column; gap: 14px; }
.entry {
  display: flex; justify-content: space-between; align-items: center; gap: 24px;
  background: #12121f; border-radius: 12px; padding: 20px 24px;
}
.entry-name { font-size: 22px; font-weight: bold; }
.entry-desc { font-size: 15px; color: #9a9ab0; margin-top: 4px; }
details summary {
  cursor: pointer; color: #9a9ab0; font-size: 15px; padding: 4px 0;
}
pre.log {
  background: #08080c; border-radius: 8px; padding: 14px; overflow: auto;
  max-height: 320px; font-size: 13px; color: #9a9ab0; white-space: pre-wrap;
}
"""


def _page(title, body):
    return f"""<!DOCTYPE html>
<html lang="ja">
  <head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>{html.escape(title)}</title>
    <style>{PAGE_CSS}</style>
  </head>
  <body>
{body}
  </body>
</html>
"""


def menu_page(config):
    """Render the entry chooser (used when several metadata sets exist)."""
    rows = []
    for entry in config.entries:
        rows.append(
            f"""
        <div class="entry">
          <div>
            <div class="entry-name">{html.escape(str(entry.get("name", entry["id"])))}</div>
            <div class="entry-desc">{html.escape(str(entry.get("description", "")))}</div>
          </div>
          <form method="post" action="/start">
            <input type="hidden" name="entry" value="{html.escape(str(entry["id"]))}">
            <button type="submit">開始</button>
          </form>
        </div>"""
        )
    body = f"""
    <h1>OpenArm データ収集</h1>
    <div class="card">
      <div class="sub">実施する収集を選んでください。</div>
      <div class="entry-list">{"".join(rows)}</div>
      <form method="post" action="/cleanup"
            onsubmit="return confirm('収集に関するプロセスをすべて強制停止します。よろしいですか？')">
        <button type="submit" class="secondary">すべて強制停止</button>
      </form>
    </div>
"""
    return _page("OpenArm データ収集 — 起動メニュー", body)


STATUS_SCRIPT = """
async function poll() {
  let s;
  try {
    s = await (await fetch("/status", {cache: "no-store"})).json();
  } catch (e) {
    setTimeout(poll, 1000);
    return;
  }
  if (s.state === "running") {
    window.location.href = s.ui_url;
    return;
  }
  render(s);
  setTimeout(poll, 1000);
}

function render(s) {
  const root = document.getElementById("root");
  if (s.state === "error") {
    root.innerHTML = errorHtml(s);
  } else if (s.state === "stopped") {
    root.innerHTML = stoppedHtml(s);
  } else {
    root.innerHTML = preparingHtml(s);
  }
}

function esc(t) {
  const d = document.createElement("div");
  d.textContent = t == null ? "" : String(t);
  return d.innerHTML;
}

function preparingHtml(s) {
  return `
    <div class="card">
      <div style="display:flex;align-items:center;gap:20px">
        <div class="spinner"></div>
        <div>
          <div class="step">${esc(s.step)}</div>
          <div class="sub">起動が終わると自動でタスク画面に切り替わります。しばらくお待ちください。</div>
        </div>
      </div>
      ${logHtml(s)}
    </div>`;
}

function errorHtml(s) {
  const items = (s.errors || []).map(e =>
    `<li>[${esc(e.time)}] ${esc(e.text)}</li>`).join("");
  return `
    <div class="card error-card">
      <div class="error-title">⚠ エラーが発生しました</div>
      <div class="step" style="color:#ff8fa3">${esc(s.step)}</div>
      ${s.hint ? `<div class="error-hint">${esc(s.hint)}</div>` : ""}
      ${items ? `<ul class="error-list">${items}</ul>` : ""}
      <div class="buttons">
        <form method="post" action="/start">
          <input type="hidden" name="entry" value="${esc(s.entry ? s.entry.id : "")}">
          <button type="submit">再試行</button>
        </form>
        <a class="button secondary" href="/log" target="_blank">詳しいログを見る</a>
        ${cleanupButton()}
        <a class="button secondary" href="/menu">メニューに戻る</a>
      </div>
      ${logHtml(s)}
    </div>`;
}

function stoppedHtml(s) {
  return `
    <div class="card">
      <div class="step">終了しました</div>
      <div class="sub">データ収集を終了しました。もう一度始めるには「再開」を押してください。</div>
      <div class="buttons">
        <form method="post" action="/start">
          <input type="hidden" name="entry" value="${esc(s.entry ? s.entry.id : "")}">
          <button type="submit">再開</button>
        </form>
        ${cleanupButton()}
        <a class="button secondary" href="/menu">メニューに戻る</a>
      </div>
      ${logHtml(s)}
    </div>`;
}

function cleanupButton() {
  return `
    <form method="post" action="/cleanup"
          onsubmit="return confirm('収集に関するプロセスをすべて強制停止します。よろしいですか？')">
      <button type="submit" class="secondary">すべて強制停止</button>
    </form>`;
}

function logHtml(s) {
  const tail = (s.log_tail || []).join("\\n");
  if (!tail) return "";
  return `<details><summary>技術ログを表示（担当者向け）</summary>
    <pre class="log">${esc(tail)}</pre></details>`;
}

poll();
"""


def status_page(runner):
    """Render the live status page (preparing / error / stopped)."""
    snapshot = runner.snapshot()
    entry_name = (snapshot["entry"] or {}).get("name", "")
    body = f"""
    <h1>OpenArm データ収集{" — " + html.escape(str(entry_name)) if entry_name else ""}</h1>
    <div id="root"></div>
    <script>{STATUS_SCRIPT}</script>
"""
    return _page("OpenArm データ収集", body)


class Handler(BaseHTTPRequestHandler):
    """HTTP endpoints of the launcher."""

    server_version = "OpenArmLauncher/1.0"

    @property
    def runner(self):
        return self.server.runner

    @property
    def config(self):
        return self.server.runner.config

    def log_message(self, format, *args):  # noqa: A002 - BaseHTTPRequestHandler API
        """Silence the default stderr access log."""

    def _send(self, code, content_type, body, extra_headers=()):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for key, value in extra_headers:
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def _redirect(self, location):
        self.send_response(303)
        self.send_header("Location", location)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self):  # noqa: N802 - BaseHTTPRequestHandler API
        """Serve the status page, the menu, the JSON status and the log."""
        path = urlparse(self.path).path
        if path == "/":
            state = self.runner.state
            if state == "idle" and len(self.config.entries) > 1:
                self._send(200, "text/html; charset=utf-8", menu_page(self.config))
            elif state == "idle":
                self.runner.start(self.config.entries[0]["id"])
                self._send(200, "text/html; charset=utf-8", status_page(self.runner))
            else:
                self._send(200, "text/html; charset=utf-8", status_page(self.runner))
        elif path == "/menu":
            self._send(200, "text/html; charset=utf-8", menu_page(self.config))
        elif path == "/status":
            snapshot = self.runner.snapshot()
            self._send(
                200,
                "application/json; charset=utf-8",
                json.dumps(snapshot, ensure_ascii=False),
                # The task screen (another port) polls this when it loses its stream.
                extra_headers=[("Access-Control-Allow-Origin", "*")],
            )
        elif path == "/log":
            snapshot = self.runner.snapshot()
            log_path = snapshot["log_path"]
            if log_path and pathlib.Path(log_path).exists():
                text = pathlib.Path(log_path).read_text(encoding="utf-8", errors="replace")
            else:
                text = "\n".join(self.runner.log)
            self._send(200, "text/plain; charset=utf-8", text)
        elif path == "/health":
            self._send(200, "text/plain; charset=utf-8", "ok")
        else:
            self._send(404, "text/plain; charset=utf-8", "not found")

    def do_POST(self):  # noqa: N802 - BaseHTTPRequestHandler API
        """Handle start / stop requests from the pages."""
        path = urlparse(self.path).path
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length).decode("utf-8") if length else ""
        form = parse_qs(raw)
        if path == "/start":
            entry_id = (form.get("entry") or [""])[0] or self.config.entries[0]["id"]
            self.runner.start(entry_id)
            self._redirect("/")
        elif path == "/stop":
            self.runner.stop()
            self._redirect("/")
        elif path == "/cleanup":
            # "Kill everything": the operator's way out of a stuck state.
            self.runner.force_stop_all()
            self._redirect("/")
        else:
            self._send(404, "text/plain; charset=utf-8", "not found")


def open_browser(url):
    """Open the operator's browser without touching a terminal."""
    for argv in (["xdg-open", url], ["gio", "open", url], ["firefox", url]):
        if shutil.which(argv[0]):
            subprocess.Popen(
                argv, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
            )
            return True
    return False


def port_in_use(port):
    """Return True when something already listens on the launcher port."""
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.3):
            return True
    except OSError:
        return False


def retry_running_instance(url, entry_id):
    """Ask an already-running launcher to start `entry_id` when it is idle."""
    if not entry_id:
        return False
    try:
        with urllib.request.urlopen(url.rstrip("/") + "/status", timeout=2) as response:
            state = json.loads(response.read().decode("utf-8")).get("state")
    except (OSError, ValueError):
        return False
    if state not in ("idle", "stopped", "error"):
        return False
    try:
        request = urllib.request.Request(
            url.rstrip("/") + "/start",
            data=f"entry={entry_id}".encode(),
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            method="POST",
        )
        urllib.request.urlopen(request, timeout=5).read()
    except OSError:
        return False
    return True


def other_launcher_pids():
    """Return the pids of other running launcher processes."""
    me = pathlib.Path(__file__).name
    pids = []
    for entry in pathlib.Path("/proc").iterdir():
        if not entry.name.isdigit() or int(entry.name) == os.getpid():
            continue
        argv = _read_cmdline(int(entry.name))
        if any(pathlib.PurePath(arg).name == me for arg in argv[:3]):
            pids.append(int(entry.name))
    return pids


def kill_everything(config, port):
    """Stop the dataflow and every process left behind (the `--kill` entry)."""
    url = f"http://127.0.0.1:{port}/"
    answered = False
    if port_in_use(port):
        try:
            request = urllib.request.Request(url + "stop", data=b"", method="POST")
            urllib.request.urlopen(request, timeout=30).read()
            answered = True
        except OSError:
            pass
    killed = len(kill_pids(stale_dataflow_pids(config.repo_dir)))
    # The launcher goes down too, whether it answered or is wedged: "stop
    # everything" must leave nothing holding the port, and the shortcut starts
    # a fresh one (with the current code) on the next double-click.
    killed += len(kill_pids(other_launcher_pids()))
    return answered, killed


def main():
    """Run the launcher web server and, optionally, start an entry at once."""
    parser = argparse.ArgumentParser(description="OpenArm データ収集ランチャー")
    default_config = pathlib.Path(__file__).resolve().parent / "launcher.yaml"
    parser.add_argument("--config", default=str(default_config), help="launcher.yaml")
    parser.add_argument("--entry", help="起動する entry の id")
    parser.add_argument("--port", type=int, help="待ち受けポート")
    parser.add_argument(
        "--no-browser", action="store_true", help="ブラウザを自動で開かない"
    )
    parser.add_argument(
        "--kill",
        action="store_true",
        help="収集に関するプロセスをすべて停止して終了する",
    )
    args = parser.parse_args()

    config = Config(args.config)
    port = args.port or config.port
    url = f"http://127.0.0.1:{port}/"

    if args.kill:
        answered, killed = kill_everything(config, port)
        if answered:
            detail = "実行中のデータ収集を停止しました。"
            if killed:
                detail += f"（残っていたプロセス {killed} 件も停止しました）"
        elif killed:
            detail = f"残っていたプロセスを停止しました（{killed} 件）。"
        else:
            detail = "停止するものはありませんでした。"
        show_desktop_message(
            detail + "\n\nもう一度始めるには、デスクトップのショートカットを"
            "ダブルクリックしてください。"
        )
        return 0

    if port_in_use(port):
        # Already running: bring its page up. A second click is also how the
        # operator retries, so restart the entry unless it is busy.
        entry_id = args.entry or (
            config.entries[0]["id"] if len(config.entries) == 1 else None
        )
        retried = retry_running_instance(url, entry_id)
        if not args.no_browser:
            open_browser(url)
        print(
            f"ランチャーはすでに起動しています: {url}"
            + ("（再試行しました）" if retried else "")
        )
        return 0

    runner = Runner(config)
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    server.runner = runner
    server.daemon_threads = True

    stopping = threading.Event()

    def shutdown(signum, frame):
        stopping.set()
        runner.stop()
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    threading.Thread(target=server.serve_forever, daemon=True).start()
    print(f"ランチャーを起動しました: {url}")

    if args.entry:
        ok, message = runner.start(args.entry)
        if not ok:
            print(message, file=sys.stderr)
    elif len(config.entries) == 1:
        runner.start(config.entries[0]["id"])

    if not args.no_browser:
        open_browser(url)

    try:
        # SIGTERM (`pkill openarm_launcher.py`) must end the process, otherwise
        # a stale instance keeps the port and the shortcut looks unresponsive.
        while not stopping.wait(0.5):
            pass
    except KeyboardInterrupt:
        runner.stop()
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except BaseException as error:  # noqa: BLE001 - last resort for a shortcut
        show_desktop_error(
            "データ収集を起動できませんでした。\n\n"
            f"{type(error).__name__}: {error}\n\n"
            "担当者に連絡してください。"
        )
        raise
