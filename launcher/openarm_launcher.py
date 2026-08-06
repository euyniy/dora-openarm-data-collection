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
3. runs `dora build` when needed and then `dora run <dataflow>`,
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

try:
    import yaml
except ModuleNotFoundError:  # pragma: no cover - configuration error
    sys.stderr.write(
        "PyYAML が見つかりません。`sudo apt install python3-yaml` "
        "または `pip install pyyaml` を実行してください。\n"
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

    def dora_command(self):
        """Return the dora executable to use."""
        if self.venv:
            candidate = pathlib.Path(self.venv).expanduser() / "bin" / "dora"
            if candidate.exists():
                return str(candidate)
        found = shutil.which("dora")
        if found:
            return found
        candidate = self.repo_dir / ".venv" / "bin" / "dora"
        if candidate.exists():
            return str(candidate)
        return None


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

    def _build(self, entry, dataflow_path, dora):
        if not entry.get("build", True):
            return True
        stamp, current = self._build_stamp(entry, dataflow_path)
        if stamp.exists() and stamp.read_text(encoding="utf-8").strip() == current:
            return True
        self._set(step="ノードを準備しています…（初回は数分かかります）")
        code = self._run_step([dora, "build", str(dataflow_path)])
        if code != 0:
            self._fail(
                f"ノードの準備 (dora build) に失敗しました (終了コード {code})",
                hint="ネットワーク接続を確認して「再試行」を押してください。",
            )
            return False
        stamp.write_text(current, encoding="utf-8")
        return True

    def _prepare_dataflow(self, entry):
        """Return the dataflow to run, rewriting METADATA_FILE when overridden."""
        dataflow_path = self.config.repo_dir / entry["dataflow"]
        if not dataflow_path.exists():
            self._fail(
                f"データフロー定義が見つかりません: {dataflow_path}",
                hint="launcher/launcher.yaml の dataflow を確認してください。",
            )
            return None
        metadata = entry.get("metadata")
        if not metadata:
            return dataflow_path
        if not (self.config.repo_dir / metadata).exists():
            self._fail(
                f"メタデータが見つかりません: {self.config.repo_dir / metadata}",
                hint="launcher/launcher.yaml の metadata を確認してください。",
            )
            return None
        dataflow = yaml.safe_load(dataflow_path.read_text(encoding="utf-8"))
        changed = False
        for node in dataflow.get("nodes", []):
            env = node.get("env") or {}
            if "METADATA_FILE" in env and env["METADATA_FILE"] != metadata:
                env["METADATA_FILE"] = metadata
                node["env"] = env
                changed = True
        if not changed:
            return dataflow_path
        generated = self.config.repo_dir / f".launcher-{entry['id']}.yaml"
        generated.write_text(
            yaml.safe_dump(dataflow, sort_keys=False, allow_unicode=True),
            encoding="utf-8",
        )
        self._write(f"metadata を {metadata} に差し替えた {generated.name} を生成しました")
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
        dora = self.config.dora_command()
        if dora is None:
            self._fail(
                "dora コマンドが見つかりません",
                hint="launcher/launcher.yaml の venv に dora のある仮想環境を指定してください。",
            )
            return
        if entry.get("can_setup", True) and not self._setup_can():
            return
        dataflow_path = self._prepare_dataflow(entry)
        if dataflow_path is None:
            return
        if not self._build(entry, dataflow_path, dora):
            return

        self._set(step="データフローを起動しています…")
        env = dict(os.environ)
        env.setdefault("PYTHONUNBUFFERED", "1")
        try:
            self.proc = subprocess.Popen(
                [dora, "run", str(dataflow_path)],
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

    def stop(self):
        """Stop the running dataflow the way Ctrl-C would."""
        proc = self.proc
        # A failure already explained on screen must survive the cleanup.
        keep_error = self.state == "error"
        if proc is None or proc.poll() is not None:
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
        if proc.poll() is None:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
            except OSError:
                pass
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
        <a class="button secondary" href="/">メニューに戻る</a>
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
        <a class="button secondary" href="/">メニューに戻る</a>
      </div>
      ${logHtml(s)}
    </div>`;
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
    args = parser.parse_args()

    config = Config(args.config)
    port = args.port or config.port
    url = f"http://127.0.0.1:{port}/"

    if port_in_use(port):
        # Already running: just bring its window forward.
        if not args.no_browser:
            open_browser(url)
        print(f"ランチャーはすでに起動しています: {url}")
        return 0

    runner = Runner(config)
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    server.runner = runner
    server.daemon_threads = True

    def shutdown(signum, frame):
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
        while True:
            time.sleep(0.5)
    except KeyboardInterrupt:
        runner.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
