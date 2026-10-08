"""Sessions Monitor for Claude Code: a small Windows widget that lists every running Claude Code
session with its consumption and lets you close one, after an explicit confirmation.

Unofficial project, not affiliated with Anthropic.

Safety: the monitor is read-only. It never starts sessions and is not the parent of their
processes, so closing its window cannot affect any session. The only place where a process is
terminated is `terminate_session`, reached only after the user confirms in the dialog.

Data sources (all read-only, none of them is a documented interface and may change):
  ~/.claude/sessions/<pid>.json    live session (pid, sessionId, cwd, name, status, procStart)
  ~/.claude/projects/*/<id>.jsonl  transcript: tokens, model, permission mode, pending tool calls
  ~/.claude/usage-snapshot.json    plan limits, written by statusline-usage.js (optional)
  Win32 (ctypes)                   CPU and RAM of the process and of its children (MCP servers...)

Usage: pythonw claude_monitor.pyw [--lang it|en] [--demo] [--version]
"""
import argparse
import ctypes
import json
import math
import os
import random
import re
import subprocess
import threading
import time
import tkinter as tk
import tkinter.font as tkfont
from collections import deque
from ctypes import wintypes
from datetime import datetime
from pathlib import Path

__version__ = "1.0.0"

HERE = Path(__file__).resolve().parent
CLAUDE_DIR = Path.home() / ".claude"
SESSIONS_DIR = CLAUDE_DIR / "sessions"
PROJECTS_DIR = CLAUDE_DIR / "projects"
USAGE_FILE = CLAUDE_DIR / "usage-snapshot.json"
CONFIG = Path(os.environ.get("APPDATA", Path.home())) / "claude-sessions-monitor" / "config.json"
ICON = HERE / "assets" / "icon.ico"
TAB_SCRIPT = HERE / "focus-tab.ps1"
REFRESH_SECONDS = 2.0
NCPU = os.cpu_count() or 1
DEMO = False  # set by --demo: fake sessions, and every action that touches the system is disabled

# ---------------------------------------------------------------- Translations

STRINGS = {
    "en": {
        "title": "Sessions Monitor",
        "pinned": "always on top",
        "empty": "No active sessions",
        "session": "session", "week": "week",
        "context": "context", "output": "output",
        "working": "working", "idle": "idle", "waiting": "waiting",
        "wait_question": "question", "wait_plan": "plan", "wait_permission": "permission {tool}",
        "terminal": "terminal", "folder": "folder",
        "summary": "{n} sessions", "summary_waiting": "{n} sessions · {w} waiting",
        "alert": "{w} waiting",
        "now": "now",
        "terminal_missing": "terminal not found", "close_failed": "could not close",
        "close_title": "Close session", "close_ask": "Close {name}?",
        "close_warn": "The session and its terminal will be closed.\nWork in progress will be interrupted.",
        "cancel": "Cancel", "close_confirm": "Close session",
        "days": ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"), "day_unit": "d",
    },
    "it": {
        "title": "Sessions Monitor",
        "pinned": "in primo piano",
        "empty": "Nessuna sessione attiva",
        "session": "sessione", "week": "settimana",
        "context": "contesto", "output": "output",
        "working": "in corso", "idle": "terminata", "waiting": "in attesa",
        "wait_question": "domanda", "wait_plan": "piano", "wait_permission": "permesso {tool}",
        "terminal": "terminale", "folder": "cartella",
        "summary": "{n} sessioni", "summary_waiting": "{n} sessioni · {w} in attesa",
        "alert": "{w} in attesa",
        "now": "ora",
        "terminal_missing": "terminale non trovato", "close_failed": "chiusura non riuscita",
        "close_title": "Chiudi sessione", "close_ask": "Chiudere {name}?",
        "close_warn": "La sessione e il suo terminale verranno chiusi.\nIl lavoro in corso verrà interrotto.",
        "cancel": "Annulla", "close_confirm": "Chiudi sessione",
        "days": ("lun", "mar", "mer", "gio", "ven", "sab", "dom"), "day_unit": "g",
    },
}


def detect_language():
    """Italian if Windows' display language is Italian, English otherwise."""
    try:
        lang_id = ctypes.windll.kernel32.GetUserDefaultUILanguage()
    except (AttributeError, OSError):
        return "en"
    return "it" if lang_id & 0x3FF == 0x10 else "en"  # 0x10 = LANG_ITALIAN


LANG = detect_language()


def t(key, **kwargs):
    text = STRINGS.get(LANG, STRINGS["en"]).get(key, STRINGS["en"][key])
    return text.format(**kwargs) if kwargs else text


# ---------------------------------------------------------------- Win32

k32 = ctypes.WinDLL("kernel32", use_last_error=True)
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
TH32CS_SNAPPROCESS = 0x2
CREATE_NO_WINDOW = 0x08000000
INVALID_HANDLE = ctypes.c_void_p(-1).value

k32.OpenProcess.restype = wintypes.HANDLE
k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
k32.CloseHandle.argtypes = [wintypes.HANDLE]
k32.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
k32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
k32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
k32.Process32FirstW.argtypes = k32.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.c_void_p]


class PROCESSENTRY32W(ctypes.Structure):
    _fields_ = [
        ("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD),
        ("th32ProcessID", wintypes.DWORD), ("th32DefaultHeapID", ctypes.c_size_t),
        ("th32ModuleID", wintypes.DWORD), ("cntThreads", wintypes.DWORD),
        ("th32ParentProcessID", wintypes.DWORD), ("pcPriClassBase", wintypes.LONG),
        ("dwFlags", wintypes.DWORD), ("szExeFile", wintypes.WCHAR * 260),
    ]


class PROCESS_MEMORY_COUNTERS(ctypes.Structure):
    _fields_ = [
        ("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD),
        ("PeakWorkingSetSize", ctypes.c_size_t), ("WorkingSetSize", ctypes.c_size_t),
        ("QuotaPeakPagedPoolUsage", ctypes.c_size_t), ("QuotaPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t), ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
        ("PagefileUsage", ctypes.c_size_t), ("PeakPagefileUsage", ctypes.c_size_t),
    ]


k32.K32GetProcessMemoryInfo.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESS_MEMORY_COUNTERS), wintypes.DWORD]


def _ft(ft):
    return (ft.dwHighDateTime << 32) | ft.dwLowDateTime


def proc_info(pid):
    """(creation FILETIME, CPU time in 100 ns, working set in bytes), or None if the process is gone."""
    h = k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not h:
        return None
    try:
        c, e, k, u = (wintypes.FILETIME() for _ in range(4))
        if not k32.GetProcessTimes(h, c, e, k, u):
            return None
        if _ft(e) != 0:  # already exited (the handle is still valid)
            return None
        mem = PROCESS_MEMORY_COUNTERS()
        mem.cb = ctypes.sizeof(mem)
        ws = mem.WorkingSetSize if k32.K32GetProcessMemoryInfo(h, mem, mem.cb) else 0
        return _ft(c), _ft(k) + _ft(u), ws
    finally:
        k32.CloseHandle(h)


def process_table():
    """{pid: (parent pid, exe name)} from a process snapshot."""
    snap = k32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    out = {}
    if not snap or snap == INVALID_HANDLE:
        return out
    try:
        entry = PROCESSENTRY32W()
        entry.dwSize = ctypes.sizeof(entry)
        ok = k32.Process32FirstW(snap, ctypes.byref(entry))
        while ok:
            out[entry.th32ProcessID] = (entry.th32ParentProcessID, entry.szExeFile)
            ok = k32.Process32NextW(snap, ctypes.byref(entry))
    finally:
        k32.CloseHandle(snap)
    return out


def children_map():
    out = {}
    for pid, (ppid, _) in process_table().items():
        out.setdefault(ppid, []).append(pid)
    return out


def descendants(root, cmap):
    seen, stack = [], [root]
    while stack:
        p = stack.pop()
        if p in seen:
            continue
        seen.append(p)
        stack.extend(cmap.get(p, []))
    return seen


def same_process(pid, proc_start):
    """True if the pid is still the original process (guards against pid reuse)."""
    info = proc_info(pid)
    if not info:
        return False
    try:
        return abs(info[0] - int(proc_start)) < 20_000_000  # 2 s tolerance
    except (TypeError, ValueError):
        return True


# ---------------------------------------------------------------- Transcript

class Transcript:
    """Incremental reader of a session's jsonl: context, output tokens, model, mode, pending tools."""

    def __init__(self):
        self.path = None
        self.offset = 0
        self.output = {}    # response id -> output_tokens (content blocks repeat the same usage)
        self.context = 0
        self.model = ""
        self.title = ""
        self.mode = ""      # permission mode (plan, auto, acceptEdits, ...)
        self.pending = {}   # tool_use without a result: id -> (name, epoch)

    def track_tools(self, obj, msg):
        """A tool call without a result is either running or waiting for the user."""
        content = msg.get("content")
        if obj.get("isSidechain") or not isinstance(content, list):
            return
        try:
            ts = datetime.fromisoformat(obj["timestamp"].replace("Z", "+00:00")).timestamp()
        except (KeyError, ValueError, AttributeError):
            ts = time.time()
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "tool_use":
                self.pending[block.get("id")] = (block.get("name", ""), ts)
            elif block.get("type") == "tool_result":
                self.pending.pop(block.get("tool_use_id"), None)

    def locate(self, session_id, cwd):
        slug = re.sub(r"[^A-Za-z0-9]", "-", cwd or "")
        p = PROJECTS_DIR / slug / f"{session_id}.jsonl"
        if p.exists():
            return p
        for d in PROJECTS_DIR.iterdir() if PROJECTS_DIR.exists() else []:
            p = d / f"{session_id}.jsonl"
            if p.exists():
                return p
        return None

    def update(self, session_id, cwd):
        if self.path is None:
            self.path = self.locate(session_id, cwd)
            if self.path is None:
                return
        try:
            size = self.path.stat().st_size
            if size < self.offset:
                self.offset, self.output, self.context, self.pending = 0, {}, 0, {}
            if size == self.offset:
                return
            with open(self.path, "rb") as f:
                f.seek(self.offset)
                data = f.read()
        except OSError:
            return
        end = data.rfind(b"\n")
        if end < 0:
            return
        self.offset += end + 1
        for line in data[:end].split(b"\n"):
            if not any(k in line for k in (b'"usage"', b'"ai-title"', b'"permission-mode"', b'"tool_result"')):
                continue
            try:
                obj = json.loads(line)
            except ValueError:
                continue
            kind = obj.get("type")
            if kind == "ai-title":
                self.title = obj.get("aiTitle") or self.title
                continue
            if kind == "permission-mode":
                self.mode = obj.get("permissionMode") or self.mode
                continue
            msg = obj.get("message")
            if not isinstance(msg, dict):
                continue
            self.track_tools(obj, msg)
            if kind != "assistant":
                continue
            usage = msg.get("usage") or {}
            key = msg.get("id") or obj.get("requestId") or obj.get("uuid")
            self.output[key] = usage.get("output_tokens", 0)
            model = msg.get("model") or ""
            if model and not model.startswith("<"):  # skips "<synthetic>"
                self.model = model
            ctx = (usage.get("input_tokens", 0) + usage.get("cache_creation_input_tokens", 0)
                   + usage.get("cache_read_input_tokens", 0))
            if ctx and not obj.get("isSidechain"):
                self.context = ctx

    @property
    def total_output(self):
        return sum(self.output.values())


# ---------------------------------------------------------------- Data collection

class Collector(threading.Thread):
    """Background thread that builds a snapshot of all live sessions every REFRESH_SECONDS."""

    def __init__(self):
        super().__init__(daemon=True)
        self.snapshot = []
        self.version = 0
        self._cpu_prev = {}   # pid -> cumulative CPU time
        self._last = time.time()
        self._transcripts = {}

    def run(self):
        while True:
            try:
                self._collect()
            except Exception:
                pass
            time.sleep(REFRESH_SECONDS)

    def _collect(self):
        now = time.time()
        wall = max(now - self._last, 0.001)
        self._last = now
        cmap = children_map()
        rows, cpu_now = [], {}
        for f in SESSIONS_DIR.glob("*.json"):
            try:
                s = json.loads(f.read_text(encoding="utf-8"))
                pid = int(s["pid"])
            except (OSError, ValueError, KeyError):
                continue
            if not same_process(pid, s.get("procStart")):
                continue  # stale file left by a session that is gone
            tree = descendants(pid, cmap)
            cpu_delta, ram = 0, 0
            for p in tree:
                info = proc_info(p)
                if not info:
                    continue
                ram += info[2]
                cpu_now[p] = info[1]
                if p in self._cpu_prev:
                    cpu_delta += max(info[1] - self._cpu_prev[p], 0)
            tr = self._transcripts.setdefault(s["sessionId"], Transcript())
            tr.update(s["sessionId"], s.get("cwd"))
            cpu = cpu_delta / 1e7 / wall / NCPU * 100
            rows.append({
                "wait": self._waiting(s.get("status"), tr, cpu, now),
                "mode": tr.mode,
                "pid": pid,
                "procStart": s.get("procStart"),
                "name": s.get("name") or str(pid),
                "cwd": s.get("cwd") or "",
                "status": s.get("status") or "",
                "started": s.get("startedAt") or 0,
                "cpu": cpu,
                "ram": ram,
                "context": tr.context,
                "output": tr.total_output,
                "model": tr.model,
                "title": tr.title,
            })
        self._cpu_prev = cpu_now
        rows.sort(key=lambda r: r["started"])
        self.snapshot = rows
        self.version += 1

    @staticmethod
    def _waiting(status, tr, cpu, now):
        """What the user is being waited for, as (kind, tool), or None.

        The session file only says busy/idle. A question or a plan approval is certain; for any other
        tool the permission prompt is inferred: a call without a result for several seconds while the
        process is idle. It can be wrong (for example a long command that is just waiting on the network).
        """
        if status != "busy" or not tr.pending:
            return None
        name, ts = min(tr.pending.values(), key=lambda v: v[1])
        age = now - ts
        if name == "AskUserQuestion":
            return ("question", name)
        if name == "ExitPlanMode":
            return ("plan", name) if age > 2 else None
        if age > 8 and cpu < 5:
            return ("permission", name.rsplit("__", 1)[-1])
        return None


class DemoCollector(Collector):
    """Fake sessions for screenshots and for trying the interface (--demo). Touches nothing real."""

    SESSIONS = [
        ("web-shop", r"C:\Users\demo\dev\web-shop", "claude-opus-5-5", "plan", "busy", None),
        ("api-gateway", r"C:\Users\demo\dev\api-gateway", "claude-sonnet-5-5", "auto", "busy", ("permission", "Bash")),
        ("mobile-app", r"C:\Users\demo\dev\mobile-app", "claude-sonnet-5-5", "acceptEdits", "idle", None),
        ("docs-site", r"C:\Users\demo\dev\docs-site", "claude-opus-5-5", "default", "busy", None),
    ]

    def __init__(self):
        super().__init__()
        self.rng = random.Random(7)
        self.state = [{"cpu": self.rng.uniform(2, 20), "ram": self.rng.uniform(300, 600),
                       "context": self.rng.uniform(40_000, 300_000), "output": self.rng.uniform(5_000, 90_000)}
                      for _ in self.SESSIONS]

    def _collect(self):
        rows = []
        for i, ((name, cwd, model, mode, status, wait), st) in enumerate(zip(self.SESSIONS, self.state)):
            busy = status == "busy" and not wait
            st["cpu"] = min(max(st["cpu"] + self.rng.uniform(-8, 8) * (1 if busy else 0.1), 0.3), 70)
            st["ram"] = min(max(st["ram"] + self.rng.uniform(-12, 14), 250), 900)
            st["context"] += self.rng.uniform(0, 3500) if busy else 0
            st["output"] += self.rng.uniform(0, 600) if busy else 0
            rows.append({"wait": wait, "mode": mode, "pid": 1000 + i, "procStart": None, "name": name,
                         "cwd": cwd, "status": status, "started": i, "cpu": st["cpu"],
                         "ram": int(st["ram"] * 2**20), "context": int(st["context"]),
                         "output": int(st["output"]), "model": model, "title": name})
        self.snapshot = rows
        self.version += 1


user32 = ctypes.WinDLL("user32", use_last_error=True)
WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
user32.EnumWindows.argtypes = [WNDENUMPROC, wintypes.LPARAM]
user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
user32.IsWindowVisible.argtypes = user32.IsIconic.argtypes = user32.SetForegroundWindow.argtypes = [wintypes.HWND]
user32.GetWindow.restype = wintypes.HWND
user32.GetWindow.argtypes = [wintypes.HWND, wintypes.UINT]
user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]


def windows_of(pid):
    """Visible top-level windows of a process."""
    out = []

    def cb(h, _):
        owner = wintypes.DWORD()
        user32.GetWindowThreadProcessId(h, ctypes.byref(owner))
        if owner.value == pid and user32.IsWindowVisible(h) and not user32.GetWindow(h, 4):  # GW_OWNER
            out.append(h)
        return True

    user32.EnumWindows(WNDENUMPROC(cb), 0)
    return out


def raise_window(hwnd):
    if user32.IsIconic(hwnd):
        user32.ShowWindow(hwnd, 9)  # SW_RESTORE
    user32.keybd_event(0x12, 0, 0, 0)  # a synthetic Alt press unlocks SetForegroundWindow
    user32.keybd_event(0x12, 0, 2, 0)
    user32.SetForegroundWindow(hwnd)


def select_tab(wt_pid, title):
    """Select the Windows Terminal tab whose title contains `title`; returns its window handle."""
    if not title:
        return None
    try:
        r = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-File", str(TAB_SCRIPT),
             "-WtPid", str(wt_pid), "-Title", title],
            capture_output=True, text=True, timeout=10, creationflags=CREATE_NO_WINDOW)
        return int(r.stdout.strip().splitlines()[0])
    except (OSError, subprocess.SubprocessError, ValueError, IndexError):
        return None


def focus_terminal(pid, title):
    """Bring the terminal hosting a session to the front (and its tab, in Windows Terminal)."""
    if DEMO:
        return True
    table = process_table()
    ancestors, p = [], pid
    while p in table and len(ancestors) < 12:
        p = table[p][0]
        if p in table:
            ancestors.append(p)
    host = next((a for a in ancestors if table[a][1].lower() == "windowsterminal.exe"), None)
    hwnd = None
    if host:
        hwnd = select_tab(host, title) or next(iter(windows_of(host)), None)
    else:
        for a in ancestors:
            if table[a][1].lower() == "explorer.exe":
                break
            hwnd = next(iter(windows_of(a)), None)
            if hwnd:
                break
    if hwnd:
        raise_window(hwnd)
    return bool(hwnd)


def close_terminal_tab(wt_pid, title):
    """Close the Windows Terminal tab titled `title`, only if exactly one tab matches."""
    if not title:
        return False
    try:
        r = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-File", str(TAB_SCRIPT),
             "-WtPid", str(wt_pid), "-Title", title, "-Action", "close"],
            capture_output=True, text=True, timeout=10, creationflags=CREATE_NO_WINDOW)
        return "closed" in r.stdout
    except (OSError, subprocess.SubprocessError):
        return False


k32.AttachConsole.argtypes = [wintypes.DWORD]
k32.CreateFileW.restype = wintypes.HANDLE
k32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
                            wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
k32.WriteFile.argtypes = [wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD,
                          ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p]
# Terminal modes that a TUI enables and a forced kill leaves on: mouse tracking, focus reports,
# bracketed paste, application cursor keys, alternate screen, extended keyboard protocols.
TERMINAL_RESET = ("\x1b[?1000l\x1b[?1002l\x1b[?1003l\x1b[?1005l\x1b[?1006l\x1b[?1015l\x1b[?1004l"
                  "\x1b[?2004l\x1b[?1l\x1b[?1049l\x1b[<99u\x1b[>4;0m\x1b[0m\x1b[?25h")


def write_to_console(shell_pid, text):
    """Write `text` to the console (pseudo console) that `shell_pid` is attached to."""
    k32.FreeConsole()  # the monitor has no console of its own; make sure of it before attaching
    if not k32.AttachConsole(shell_pid):
        return False
    try:
        handle = k32.CreateFileW("CONOUT$", 0xC0000000, 3, None, 3, 0, None)  # read+write, shared, open existing
        if not handle or handle == INVALID_HANDLE:
            return False
        data = text.encode("utf-8")
        written = wintypes.DWORD()
        ok = k32.WriteFile(handle, data, len(data), ctypes.byref(written), None)
        k32.CloseHandle(handle)
        return bool(ok)
    finally:
        k32.FreeConsole()


SHELLS = {"powershell.exe", "pwsh.exe", "cmd.exe", "bash.exe", "wsl.exe", "zsh.exe", "nu.exe"}


def terminate_session(pid, proc_start, title=""):
    """Stop a session, closing its terminal tab when it can be identified with certainty.

    Call only after the user confirmed. Returns True once the session process is gone.
    """
    if DEMO:
        return True
    if not same_process(pid, proc_start):
        return False
    # The tab must be found before the kill: afterwards the shell renames it.
    table = process_table()
    parent = table.get(pid, (0, ""))[0]
    shell = parent if table.get(parent, (0, ""))[1].lower() in SHELLS else None
    p, host = pid, None
    for _ in range(12):
        p = table.get(p, (0, ""))[0]
        if p not in table:
            break
        if table[p][1].lower() == "windowsterminal.exe":
            host = p
            break
    tab_closed = bool(host) and close_terminal_tab(host, title)
    # Closing the tab ends the session by itself; this makes sure (and covers other terminals).
    if same_process(pid, proc_start):
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True,
                       creationflags=CREATE_NO_WINDOW)
    gone = False
    for _ in range(20):  # up to 2 s for the process to disappear
        if not same_process(pid, proc_start):
            gone = True
            break
        time.sleep(0.1)
    # The terminal stays open: a forced kill leaves the modes of the Claude screen on (mouse tracking,
    # focus reports...) and the shell would print garbage at every mouse move. Switch them off.
    if gone and not tab_closed and shell:
        write_to_console(shell, TERMINAL_RESET)
    return gone


# ---------------------------------------------------------------- Interface: constants and helpers

BG, PANEL, LINE = "#1a1918", "#242321", "#3a3835"
FG, MUTED, ACCENT = "#faf9f5", "#8a8780", "#d97757"
OK, DANGER = "#7fb685", "#e0605a"
BUSY, WAIT = "#8ad4e8", "#f0b84b"   # working (blue), waiting for the user (amber)
OFF = "#5f5c57"                      # a filter that is switched off
# Permission mode of a session: id in the transcript -> (label, color)
MODES = {
    "default": ("default", MUTED),
    "plan": ("plan", "#8ad4e8"),
    "auto": ("auto", "#c3a6ff"),
    "acceptEdits": ("accept edits", OK),
    "bypassPermissions": ("bypass", DANGER),
}
MODEL_COLORS = {"opus": "#c3a6ff", "sonnet": "#8ad4e8", "haiku": "#7fd8be", "fable": "#f4a7d0"}
SERIES_COLORS = ("#d97757", "#8ad4e8", "#c3a6ff", "#7fd8be", "#f4a7d0", "#f0b84b")
FONT_FAMILY = "Cascadia Mono"  # replaced by the first installed monospace font in App.__init__
CONTEXT_WINDOW = 1_000_000  # scale of the context bar
HISTORY = 150               # samples kept for the charts (5 minutes at 2 s)
WIDTH = 800
MIN_WIDTH = 340
# Responsive layouts, widest first. "min" is the window width from which a layout applies; the
# narrower the window, the more columns and secondary details disappear.
LAYOUTS = {
    "full": dict(min=780, cols=("cpu", "ram", "ctx", "out"), chips=("model", "mode"), name=26,
                 path=2, summary=True, side_by_side=True, detail=True),
    "mid": dict(min=670, cols=("cpu", "ram", "ctx"), chips=("model", "mode"), name=22,
                path=2, summary=True, side_by_side=True, detail=True),
    "narrow": dict(min=535, cols=("ram", "ctx"), chips=("model",), name=18,
                   path=1, summary=False, side_by_side=True, detail=True),
    "small": dict(min=0, cols=("ctx",), chips=(), name=14,
                  path=0, summary=False, side_by_side=False, detail=False),
}


def pick_layout(width):
    return next(k for k, v in LAYOUTS.items() if width >= v["min"])


def shorten(text, n):
    return text if len(text) <= n else text[:n - 1] + "…"


def fmt_tokens(n):
    if n >= 1_000_000:
        return f"{n / 1e6:.1f}M"
    if n >= 1000:
        return f"{round(n / 1000)}k"
    return str(n) if n else "–"


def fmt_ram(b):
    return f"{b / 2**30:.1f} GB" if b >= 2**30 else f"{round(b / 2**20)} MB"


def parse_model(model):
    """claude-opus-5-5 -> ("Opus 5.5", "opus"); unknown ids are shown as they are."""
    m = re.match(r"claude-([a-z]+)-(\d+)(?:-(\d+))?", model or "")
    if not m:
        return model or "", ""
    family, major, minor = m.groups()
    return f"{family.capitalize()} {major}" + (f".{minor}" if minor else ""), family


def short_cwd(cwd, parts=2):
    names = [p for p in re.split(r"[\\/]", cwd) if p]
    return "\\".join(names[-parts:]) if names else ""


def colorref(hex_color):
    r, g, b = (int(hex_color[i:i + 2], 16) for i in (1, 3, 5))
    return r | (g << 8) | (b << 16)


def style_titlebar(root):
    """Dark title bar matching the widget (Windows 11; ignored elsewhere)."""
    try:
        hwnd = ctypes.windll.user32.GetParent(root.winfo_id())
        for attr, val in ((20, 1), (34, colorref(BG)), (35, colorref(BG)), (36, colorref(MUTED))):
            ctypes.windll.dwmapi.DwmSetWindowAttribute(
                hwnd, attr, ctypes.byref(ctypes.c_int(val)), ctypes.sizeof(ctypes.c_int))
    except Exception:
        pass


def load_config():
    try:
        return json.loads(CONFIG.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_config(cfg):
    if DEMO:  # the demo must not overwrite the real window position
        return
    try:
        CONFIG.parent.mkdir(parents=True, exist_ok=True)
        CONFIG.write_text(json.dumps(cfg), encoding="utf-8")
    except OSError:
        pass


def load_usage():
    """Plan limits as {key: (percent used, reset epoch)}; empty until statusline-usage.js has run."""
    if DEMO:
        now = time.time()
        return {"five_hour": (34, now + 2 * 3600 + 540), "seven_day": (41, now + 3 * 86400 + 9 * 3600)}
    try:
        limits = json.loads(USAGE_FILE.read_text(encoding="utf-8"))["rate_limits"]
    except (OSError, ValueError, KeyError, TypeError):
        return {}
    out = {}
    for key in ("five_hour", "seven_day"):
        w = limits.get(key) or {}
        pct, reset = w.get("used_percentage"), w.get("resets_at")
        if isinstance(reset, str):
            try:
                reset = datetime.fromisoformat(reset.replace("Z", "+00:00")).timestamp()
            except ValueError:
                reset = None
        elif isinstance(reset, (int, float)) and reset > 1e12:  # milliseconds
            reset /= 1000
        if isinstance(pct, (int, float)):
            out[key] = (pct, reset)
    return out


def fmt_remaining(seconds):
    m = max(int(seconds // 60), 0)
    d, h, m = m // 1440, m % 1440 // 60, m % 60
    return f"{d}{t('day_unit')} {h}h" if d else f"{h}h {m:02d}m" if h else f"{m}m"


def fmt_reset(epoch):
    dt = datetime.fromtimestamp(epoch)
    same_day = dt.date() == datetime.now().date()
    return dt.strftime("%H:%M") if same_day else f"{t('days')[dt.weekday()]} {dt:%H:%M}"


def nice_max(v):
    """A round upper bound (1, 2, 2.5, 5 x 10^k) for a chart axis."""
    if v <= 0:
        return 1
    p = 10 ** math.floor(math.log10(v))
    return next(m * p for m in (1, 2, 2.5, 5, 10) if m * p >= v)


# ---------------------------------------------------------------- Interface: widgets

class UsageBlock:
    """Plan usage percentage with a bar and the reset time."""

    def __init__(self, parent, title):
        self.frame = tk.Frame(parent, bg=BG)
        top = tk.Frame(self.frame, bg=BG)
        top.pack(fill="x")
        tk.Label(top, text=title, bg=BG, fg=MUTED, font=(FONT_FAMILY, 8)).pack(side="left")
        self.pct = tk.Label(top, bg=BG, fg=FG, font=(FONT_FAMILY, 9))
        self.pct.pack(side="right")
        # width=1: a Canvas asks for 378 px by default, which would stop the window from shrinking
        self.bar = tk.Canvas(self.frame, width=1, height=4, bg=BG, highlightthickness=0, bd=0)
        self.bar.pack(fill="x", pady=(3, 3))
        self.reset = tk.Label(self.frame, bg=BG, fg=MUTED, anchor="w", font=(FONT_FAMILY, 8))
        self.reset.pack(fill="x")
        self.share = 0
        self.bar.bind("<Configure>", lambda e: self.draw())

    def draw(self):
        w = self.bar.winfo_width()
        self.bar.delete("all")
        self.bar.create_rectangle(0, 0, w, 4, fill=LINE, width=0)
        self.bar.create_rectangle(0, 0, w * self.share, 4, width=0,
                                  fill=DANGER if self.share > 0.85 else ACCENT)

    def update(self, value):
        if value is None:
            self.share = 0
            self.pct.configure(text="–")
            self.reset.configure(text="")
        else:
            pct, reset = value
            self.share = min(pct / 100, 1)
            self.pct.configure(text=f"{pct:.0f}%")
            self.reset.configure(text=f"reset {fmt_reset(reset)} · {fmt_remaining(reset - time.time())}"
                                 if reset else "")
        self.draw()


class Chart:
    """Line chart over time, one series per session, with an automatic vertical scale."""

    def __init__(self, parent, title, fmt, floor):
        self.frame = tk.Frame(parent, bg=PANEL)
        tk.Label(self.frame, text=title, bg=PANEL, fg=MUTED, anchor="w",
                 font=(FONT_FAMILY, 9)).pack(fill="x", padx=14, pady=(10, 0))
        self.canvas = tk.Canvas(self.frame, width=1, height=1, bg=PANEL, highlightthickness=0, bd=0)
        self.canvas.pack(fill="both", expand=True, padx=14, pady=(6, 12))
        self.canvas.bind("<Configure>", lambda e: self.draw())
        self.title, self.fmt, self.floor, self.series = title, fmt, floor, {}

    def set(self, series):
        self.series = series
        self.draw()

    def draw(self):
        c = self.canvas
        w, h = c.winfo_width(), c.winfo_height()
        c.delete("all")
        if w < 120 or h < 70:
            return
        left, right, top, bottom = 60, 14, 14, 24
        plot_w, plot_h = w - left - right, h - top - bottom
        # The horizontal axis only covers the time collected so far (at least 1 minute), so the
        # lines spread across the width instead of being squeezed against the right edge.
        span = max([len(v) for _, v in self.series.values()] + [30])
        peak = nice_max(max([max(v) for _, v in self.series.values() if v] + [self.floor]) * 1.1)
        for i in range(5):
            y = top + plot_h * i / 4
            c.create_line(left, y, w - right, y, fill=LINE)
            c.create_text(left - 8, y, text=self.fmt(peak * (1 - i / 4)), fill=MUTED, anchor="e",
                          font=(FONT_FAMILY, 8))
        seconds = (span - 1) * REFRESH_SECONDS
        start = f"-{round(seconds / 60)} min" if seconds >= 90 else f"-{round(seconds)} s"
        c.create_text(left, h - 2, text=start, fill=MUTED, anchor="sw", font=(FONT_FAMILY, 8))
        c.create_text(w - right, h - 2, text=t("now"), fill=MUTED, anchor="se", font=(FONT_FAMILY, 8))
        for color, values in self.series.values():
            if len(values) < 2:
                continue
            offset = span - len(values)  # new samples enter from the right
            pts = []
            for k, v in enumerate(values):
                pts += [left + plot_w * (offset + k) / (span - 1), top + plot_h * (1 - min(v / peak, 1))]
            c.create_line(*pts, fill=color, width=2, joinstyle="round", capstyle="round")
            c.create_oval(pts[-2] - 4, pts[-1] - 4, pts[-2] + 4, pts[-1] + 4, fill=color, outline=PANEL, width=2)


class Row:
    COLS = (("cpu", 6), ("ram", 10), ("ctx", 9), ("out", 8))

    def __init__(self, app, parent):
        self.data = None
        self.frame = tk.Frame(parent, bg=PANEL)
        self.dot = tk.Label(self.frame, text="●", bg=PANEL, fg=MUTED, font=(FONT_FAMILY, 9))
        self.title = tk.Frame(self.frame, bg=PANEL)
        self.name = tk.Label(self.title, bg=PANEL, fg=FG, anchor="w", font=(FONT_FAMILY, 10))
        self.model = tk.Label(self.title, bg=PANEL, fg=MUTED, anchor="w", font=(FONT_FAMILY, 8))
        self.mode = tk.Label(self.title, bg=PANEL, fg=MUTED, anchor="w", font=(FONT_FAMILY, 8))
        self.name.pack(side="left")
        self.dot_color = MUTED
        self.spec = LAYOUTS[app.layout]
        self.meta = tk.Frame(self.frame, bg=PANEL)
        self.state = tk.Label(self.meta, bg=PANEL, fg=MUTED, anchor="w", font=(FONT_FAMILY, 8))
        self.state.pack(side="left")
        self.path = tk.Label(self.meta, bg=PANEL, fg=MUTED, anchor="w", font=(FONT_FAMILY, 8))
        self.path.pack(side="left", padx=(12, 0))
        self.links = []
        for text, action in ((t("terminal"), app.open_terminal), (t("folder"), app.open_folder)):
            link = tk.Label(self.meta, text=text, bg=PANEL, fg=MUTED, cursor="hand2", font=(FONT_FAMILY, 8))
            link.pack(side="left", padx=(12, 0))
            self.links.append(link)
            link.bind("<Enter>", lambda e, l=link: l.configure(fg=ACCENT))
            link.bind("<Leave>", lambda e, l=link: l.configure(fg=MUTED))
            link.bind("<Button-1>", lambda e, a=action: a(self.data))
        self.vals = {c: tk.Label(self.frame, bg=PANEL, fg=FG, anchor="e", width=w, font=(FONT_FAMILY, 9))
                     for c, w in self.COLS}
        self.close = tk.Label(self.frame, text="✕", bg=PANEL, fg=MUTED, cursor="hand2",
                              font=(FONT_FAMILY, 10), width=2, padx=8)
        self.bar = tk.Canvas(self.frame, width=1, height=3, bg=PANEL, highlightthickness=0, bd=0)
        self.dot.grid(row=0, column=0, rowspan=2, padx=(12, 6), pady=(10, 0))
        self.title.grid(row=0, column=1, sticky="we", pady=(10, 0))
        self.meta.grid(row=1, column=1, columnspan=5, sticky="w")
        for i, (c, _) in enumerate(self.COLS):
            self.vals[c].grid(row=0, column=2 + i, padx=8, pady=(10, 0))
        self.close.grid(row=0, column=6, rowspan=2, padx=(4, 6), pady=(10, 0))
        self.layout(app.layout)
        self.bar.grid(row=2, column=0, columnspan=7, sticky="we", padx=12, pady=(8, 10))
        self.frame.columnconfigure(1, weight=1)
        self.close.bind("<Enter>", lambda e: self.close.configure(fg=DANGER))
        self.close.bind("<Leave>", lambda e: self.close.configure(fg=MUTED))
        self.close.bind("<Button-1>", lambda e: app.confirm_close(self.data))
        self.bar.bind("<Configure>", lambda e: self.draw_bar())

    def layout(self, name):
        """Show only the columns and details the responsive layout allows."""
        self.spec = spec = LAYOUTS[name]
        for key, label in self.vals.items():
            (label.grid if key in spec["cols"] else label.grid_remove)()
        for chip in (self.model, self.mode):
            chip.pack_forget()
        for key in spec["chips"]:
            getattr(self, key).pack(side="left", padx=(10, 0))
        self.path.pack_forget()
        if spec["path"]:
            self.path.pack(side="left", padx=(12, 0), before=self.links[0])
        if self.data:
            self.update(self.data)

    def pulse(self, on):
        """The dot blinks while the session waits for the user."""
        if self.data and self.data["wait"]:
            self.dot.configure(fg=self.dot_color if on else PANEL)

    def draw_bar(self):
        if not self.data:
            return
        w = self.bar.winfo_width()
        share = min(self.data["context"] / CONTEXT_WINDOW, 1)
        self.bar.delete("all")
        self.bar.create_rectangle(0, 0, w, 3, fill=LINE, width=0)
        self.bar.create_rectangle(0, 0, w * share, 3, fill=DANGER if share > 0.8 else ACCENT, width=0)

    def update(self, d):
        self.data = d
        if d["wait"]:
            kind, tool = d["wait"]
            detail = f" · {t('wait_' + kind, tool=tool)}" if self.spec["detail"] else ""
            text, self.dot_color = t("waiting") + detail, WAIT
        elif d["status"] == "busy":
            text, self.dot_color = t("working"), BUSY
        elif d["status"] == "idle":
            text, self.dot_color = t("idle"), OK
        else:  # any other status (for example "shell") means the session is doing something
            text, self.dot_color = d["status"] or t("working"), BUSY
        self.dot.configure(fg=self.dot_color)
        self.state.configure(text=text, fg=self.dot_color)
        mode, mode_color = MODES.get(d["mode"], (d["mode"], MUTED))
        self.mode.configure(text=mode, fg=mode_color)
        self.name.configure(text=shorten(d["name"], self.spec["name"]))
        label, family = parse_model(d["model"])
        self.model.configure(text=label, fg=MODEL_COLORS.get(family, MUTED))
        self.path.configure(text=shorten(short_cwd(d["cwd"], max(self.spec["path"], 1)), 28))
        self.vals["cpu"].configure(text=f"{d['cpu']:.0f}%", fg=ACCENT if d["cpu"] >= 50 else FG)
        self.vals["ram"].configure(text=fmt_ram(d["ram"]))
        self.vals["ctx"].configure(text=fmt_tokens(d["context"]))
        self.vals["out"].configure(text=fmt_tokens(d["output"]), fg=MUTED)
        self.draw_bar()


class App:
    def __init__(self):
        global FONT_FAMILY
        self.cfg = load_config()
        self.root = tk.Tk()
        installed = set(tkfont.families())
        FONT_FAMILY = next((f for f in ("Cascadia Mono", "Consolas", "Courier New") if f in installed),
                           FONT_FAMILY)
        self.root.title(t("title") + (" (demo)" if DEMO else ""))
        self.root.configure(bg=BG)
        self.root.minsize(MIN_WIDTH, 100)
        screen_w, screen_h = self.root.winfo_screenwidth(), self.root.winfo_screenheight()
        # Saved values are always brought back inside the screen.
        self.width = min(max(int(self.cfg.get("width", WIDTH)), MIN_WIDTH), screen_w - 40)
        pos = self.cfg.get("pos")
        self.pos = [min(max(pos[0], 0), screen_w - 200), min(max(pos[1], 0), screen_h - 200)] if pos else None
        self.layout = pick_layout(self.width)
        try:
            self.root.iconbitmap(default=str(ICON))
        except tk.TclError:
            pass
        self.pinned = self.cfg.get("pinned", True)
        self.root.attributes("-topmost", self.pinned)
        self.rows, self.order, self.seen_version, self.tick = {}, [], -1, 0
        self.fit = 0
        # The height follows the number of sessions, but the window stays resizable and maximizable.
        self.root.resizable(True, True)

        head = tk.Frame(self.root, bg=BG)
        head.pack(fill="x", padx=16, pady=(12, 8))
        tk.Label(head, text="✻", bg=BG, fg=ACCENT, font=(FONT_FAMILY, 15)).pack(side="left")
        tk.Label(head, text="claude code", bg=BG, fg=FG, font=(FONT_FAMILY, 12)).pack(side="left", padx=(8, 0))
        # When the summary is hidden (narrow window) sessions waiting for the user are still shown here.
        self.alert = tk.Label(head, bg=BG, fg=WAIT, font=(FONT_FAMILY, 9))
        self.alert.pack(side="left", padx=(12, 0))
        self.pin = tk.Label(head, bg=BG, fg=ACCENT if self.pinned else MUTED,
                            cursor="hand2", font=(FONT_FAMILY, 9))
        self.pin.pack(side="right")
        self.draw_pin()
        self.pin.bind("<Button-1>", self.toggle_pin)
        self.summary = tk.Label(head, bg=BG, fg=MUTED, font=(FONT_FAMILY, 9))
        self.summary.pack(side="right", padx=16)

        self.usage = tk.Frame(self.root, bg=BG)
        self.usage.pack(fill="x", padx=16, pady=(0, 10))
        self.usage.columnconfigure(0, weight=1, uniform="usage")
        self.usage_blocks = {}
        for key, title in (("five_hour", t("session")), ("seven_day", t("week"))):
            self.usage_blocks[key] = UsageBlock(self.usage, title)

        cols = tk.Frame(self.root, bg=BG)
        cols.pack(fill="x", padx=16)
        cols.columnconfigure(0, weight=1)
        self.head_cols = {}
        for i, (key, text, w) in enumerate((("cpu", "cpu", 6), ("ram", "ram", 10),
                                            ("ctx", t("context"), 9), ("out", t("output"), 8))):
            label = tk.Label(cols, text=text, bg=BG, fg=MUTED, width=w, anchor="e", font=(FONT_FAMILY, 9))
            label.grid(row=0, column=1 + i, padx=8)
            self.head_cols[key] = label
        tk.Label(cols, bg=BG, width=2, padx=8, font=(FONT_FAMILY, 10)).grid(row=0, column=5, padx=(4, 6))

        self.list = tk.Frame(self.root, bg=BG)
        self.list.pack(fill="both", expand=True, padx=16, pady=(4, 16))
        self.empty = tk.Label(self.list, text=t("empty"), bg=BG, fg=MUTED, font=(FONT_FAMILY, 10), pady=24)

        # Charts only appear when the window has room to fill (maximized or very tall).
        self.charts = tk.Frame(self.root, bg=BG)
        self.legend = tk.Frame(self.charts, bg=BG)
        self.legend.pack(fill="x", pady=(0, 8))
        self.chart_grid = tk.Frame(self.charts, bg=BG)
        self.chart_grid.pack(fill="both", expand=True)
        self.chart_list = {
            "cpu": Chart(self.chart_grid, "cpu", lambda v: f"{v:.0f}%", 10),
            "ram": Chart(self.chart_grid, "ram", lambda v: f"{v / 1024:.1f} GB" if v >= 1024 else f"{v:.0f} MB", 256),
            "ctx": Chart(self.chart_grid, t("context"), lambda v: fmt_tokens(int(v)) if v else "0", 50_000),
        }
        self.show_charts, self.charts_side, self.content_h = False, None, 0
        self.history, self.colors, self.legend_sig = {}, {}, None
        self.hidden = set()  # sessions switched off in the chart filters
        enabled = self.cfg.get("charts", list(self.chart_list))
        self.chart_on = {k: k in enabled for k in self.chart_list}

        self.root.geometry(f"{self.width}x240" + (f"+{self.pos[0]}+{self.pos[1]}" if self.pos else ""))
        self.apply_layout()
        self.root.update_idletasks()
        style_titlebar(self.root)
        self.root.bind("<Configure>", self.on_configure)

        # Closing the window only destroys the monitor, never the sessions.
        self.root.protocol("WM_DELETE_WINDOW", self.quit)
        self.collector = DemoCollector() if DEMO else Collector()
        if DEMO:  # pre-fill the charts so a screenshot does not start empty
            for _ in range(90):
                self.collector._collect()
                self.record_history(self.collector.snapshot)
        self.collector.start()
        self.poll()

    def apply_layout(self):
        spec = LAYOUTS[self.layout]
        for key, label in self.head_cols.items():
            (label.grid if key in spec["cols"] else label.grid_remove)()
        if spec["summary"]:
            self.summary.pack(side="right", padx=16)
        else:
            self.summary.pack_forget()
        for i, block in enumerate(self.usage_blocks.values()):
            if spec["side_by_side"]:
                block.frame.grid(row=0, column=i, sticky="we", padx=(0, 24) if i == 0 else 0, pady=0)
            else:
                block.frame.grid(row=i, column=0, sticky="we", padx=0, pady=(0, 6) if i == 0 else 0)
        # In a single column the block uses the full width (no uniform group with the empty column).
        self.usage.columnconfigure(0, weight=1, uniform="usage" if spec["side_by_side"] else "")
        self.usage.columnconfigure(1, weight=1 if spec["side_by_side"] else 0,
                                   uniform="usage" if spec["side_by_side"] else "")
        for row in self.rows.values():
            row.layout(self.layout)

    def arrange_charts(self, side):
        """Charts side by side when the window is wide, stacked otherwise."""
        visible = [k for k in self.chart_list if self.chart_on[k]]
        for i in range(3):  # reset the weights: switched-off charts must not take space
            self.chart_grid.columnconfigure(i, weight=0, uniform="")
            self.chart_grid.rowconfigure(i, weight=0, uniform="")
        for key, chart in self.chart_list.items():
            if key not in visible:
                chart.frame.grid_remove()
                continue
            i = visible.index(key)
            last = i == len(visible) - 1
            chart.frame.grid(row=0 if side else i, column=i if side else 0, sticky="nsew",
                             padx=(0, 0 if last else 12) if side else 0, pady=0 if side or last else (0, 12))
            if side:
                self.chart_grid.columnconfigure(i, weight=1, uniform="c")
            else:
                self.chart_grid.rowconfigure(i, weight=1, uniform="r")
        if side:
            self.chart_grid.rowconfigure(0, weight=1)
        else:
            self.chart_grid.columnconfigure(0, weight=1)
        self.charts_side = side

    def update_charts(self, width, height):
        side = width >= 900
        if not self.show_charts:
            self.content_h = self.root.winfo_reqheight()
        extra = 240 if side else 380  # free space needed for the charts to be readable
        want = width >= 700 and height >= self.content_h + extra - (40 if self.show_charts else 0)
        if want and side != self.charts_side:
            self.arrange_charts(side)
        if want == self.show_charts:
            return
        self.show_charts = want
        if want:
            self.list.pack_configure(expand=False)
            self.charts.pack(fill="both", expand=True, padx=16, pady=(0, 16))
            self.refresh_charts()
        else:
            self.charts.pack_forget()
            self.list.pack_configure(expand=True)
            self.fit_height()

    def refresh_charts(self):
        if not self.show_charts:
            return
        order = [d["pid"] for d in self.collector.snapshot]
        for key, chart in self.chart_list.items():
            chart.set({pid: (self.colors[pid], list(self.history[pid][key]))
                       for pid in order if pid in self.history and pid in self.colors and pid not in self.hidden})
        sig = (tuple((d["pid"], d["name"], self.colors.get(d["pid"]), d["pid"] in self.hidden)
                     for d in self.collector.snapshot), tuple(self.chart_on.values()))
        if sig != self.legend_sig:
            self.legend_sig = sig
            self.build_filters(sig[0])

    def build_filters(self, sessions):
        """Filter bar: one entry per session (left) and one per chart (right), click to switch on/off."""
        for w in self.legend.winfo_children():
            w.destroy()

        def chip(text, color, on, command, side):
            label = tk.Label(self.legend, text=("●" if on else "○") + " " + text, bg=BG,
                             fg=color if on else OFF, cursor="hand2", font=(FONT_FAMILY, 9), padx=8, pady=4)
            label.pack(side=side)
            label.bind("<Button-1>", lambda e: command())

        for pid, name, color, hidden in sessions:
            chip(shorten(name, 24), color or MUTED, not hidden, lambda p=pid: self.toggle_session(p), "left")
        for key, chart in reversed(list(self.chart_list.items())):
            chip(chart.title, FG, self.chart_on[key], lambda k=key: self.toggle_chart(k), "right")

    def toggle_session(self, pid):
        self.hidden ^= {pid}
        self.refresh_charts()

    def toggle_chart(self, key):
        self.chart_on[key] = not self.chart_on[key]
        self.cfg["charts"] = [k for k, on in self.chart_on.items() if on]
        save_config(self.cfg)
        self.arrange_charts(self.charts_side)
        self.refresh_charts()

    def record_history(self, snap):
        live = {d["pid"] for d in snap}
        for pid in [p for p in self.history if p not in live]:
            del self.history[pid]
            self.colors.pop(pid, None)
        for d in snap:
            pid = d["pid"]
            if pid not in self.colors:
                used = set(self.colors.values())
                self.colors[pid] = next((c for c in SERIES_COLORS if c not in used), SERIES_COLORS[0])
            h = self.history.setdefault(pid, {k: deque(maxlen=HISTORY) for k in ("cpu", "ram", "ctx")})
            h["cpu"].append(d["cpu"])
            h["ram"].append(d["ram"] / 2**20)  # MB
            h["ctx"].append(d["context"])

    def on_configure(self, event):
        """Switch layout when the width crosses a threshold; show or hide the charts."""
        if event.widget is not self.root or event.width < MIN_WIDTH:
            return
        if self.root.state() == "normal":
            self.width = event.width  # the width of a maximized window is not remembered
        layout = pick_layout(event.width)
        if layout != self.layout:
            self.layout = layout
            self.apply_layout()
            self.fit_height()
        self.update_charts(event.width, event.height)

    def save(self):
        self.cfg.update(pinned=self.pinned, width=self.width)
        if self.root.state() == "normal":
            self.cfg["pos"] = [self.root.winfo_x(), self.root.winfo_y()]
        save_config(self.cfg)

    def quit(self):
        self.save()
        self.root.destroy()

    def draw_pin(self):
        self.pin.configure(text=("☑" if self.pinned else "☐") + " " + t("pinned"),
                           fg=ACCENT if self.pinned else MUTED)

    def toggle_pin(self, _=None):
        self.pinned = not self.pinned
        self.root.attributes("-topmost", self.pinned)
        self.draw_pin()
        self.save()

    def update_usage(self):
        usage = load_usage()
        for key, block in self.usage_blocks.items():
            block.update(usage.get(key))

    def open_terminal(self, d):
        def work():
            if not focus_terminal(d["pid"], d.get("title") or d["name"]):
                self.root.after(0, lambda: self.summary.configure(text=t("terminal_missing")))
        threading.Thread(target=work, daemon=True).start()

    def open_folder(self, d):
        if not DEMO and os.path.isdir(d["cwd"]):
            os.startfile(d["cwd"])

    def poll(self):
        self.tick += 1
        for row in self.rows.values():
            row.pulse(self.tick % 2 == 0)
        self.update_usage()
        if self.collector.version != self.seen_version:
            self.seen_version = self.collector.version
            self.render(self.collector.snapshot)
        self.root.after(500, self.poll)

    def fit_height(self):
        self.root.update_idletasks()
        req_h = self.root.winfo_reqheight()
        self.fit = req_h
        self.root.minsize(MIN_WIDTH, req_h)  # never shorter than the content
        if self.root.state() != "normal" or self.show_charts:
            return  # maximized (or with charts) the user decides the geometry
        w = max(self.width, MIN_WIDTH)
        x = min(self.root.winfo_x(), max(self.root.winfo_screenwidth() - w, 0))  # stay on screen
        self.root.geometry(f"{w}x{req_h}+{x}+{self.root.winfo_y()}")

    def render(self, snap):
        self.record_history(snap)
        live = {d["pid"]: d for d in snap}
        for pid in [p for p in self.rows if p not in live]:
            self.rows.pop(pid).frame.destroy()
        for pid, d in live.items():
            if pid not in self.rows:
                self.rows[pid] = Row(self, self.list)
            self.rows[pid].update(d)
        order = [d["pid"] for d in snap]
        if order != self.order:
            for r in self.rows.values():
                r.frame.pack_forget()
            for pid in order:
                self.rows[pid].frame.pack(fill="x", pady=3)
            self.order = order
            self.empty.pack_forget()
            if not order:
                self.empty.pack()
            self.fit_height()
        waiting = sum(1 for d in snap if d["wait"])
        head = t("summary_waiting", n=len(snap), w=waiting) if waiting else t("summary", n=len(snap))
        self.summary.configure(
            text=f"{head} · cpu {sum(d['cpu'] for d in snap):.0f}% · ram {fmt_ram(sum(d['ram'] for d in snap))}",
            fg=WAIT if waiting else MUTED)
        self.alert.configure(text=t("alert", w=waiting) if waiting and not LAYOUTS[self.layout]["summary"] else "")
        self.root.update_idletasks()
        if self.root.winfo_reqheight() != self.fit:
            self.fit_height()
        self.update_charts(self.root.winfo_width(), self.root.winfo_height())
        self.refresh_charts()

    def confirm_close(self, d):
        dlg = tk.Toplevel(self.root)
        dlg.title(t("close_title"))
        dlg.configure(bg=BG)
        dlg.transient(self.root)
        dlg.resizable(False, False)
        dlg.attributes("-topmost", True)
        tk.Label(dlg, text=t("close_ask", name=d["name"]), bg=BG, fg=FG, font=(FONT_FAMILY, 11),
                 padx=24).pack(anchor="w", pady=(22, 2))
        tk.Label(dlg, text=short_cwd(d["cwd"]), bg=BG, fg=MUTED, font=(FONT_FAMILY, 9),
                 padx=24).pack(anchor="w")
        tk.Label(dlg, text=t("close_warn"), bg=BG, fg=DANGER, font=(FONT_FAMILY, 9), justify="left",
                 padx=24).pack(anchor="w", pady=(10, 0))
        btns = tk.Frame(dlg, bg=BG)
        btns.pack(anchor="e", padx=24, pady=22)

        def go():
            dlg.destroy()

            def work():  # closing a tab takes about a second: keep it off the interface thread
                if not terminate_session(d["pid"], d["procStart"], d.get("title") or d["name"]):
                    self.root.after(0, lambda: self.summary.configure(text=t("close_failed")))
            threading.Thread(target=work, daemon=True).start()

        def button(text, cmd, fg, bg):
            b = tk.Label(btns, text=text, bg=bg, fg=fg, cursor="hand2", padx=16, pady=6,
                         font=(FONT_FAMILY, 9))
            b.bind("<Button-1>", lambda e: cmd())
            b.pack(side="left", padx=(8, 0))

        button(t("cancel"), dlg.destroy, FG, LINE)
        button(t("close_confirm"), go, "#ffffff", DANGER)
        dlg.bind("<Escape>", lambda e: dlg.destroy())
        dlg.update_idletasks()
        style_titlebar(dlg)
        x = self.root.winfo_rootx() + (self.root.winfo_width() - dlg.winfo_width()) // 2
        dlg.geometry(f"+{max(x, 0)}+{self.root.winfo_rooty() + 40}")
        dlg.grab_set()

    def run(self):
        self.root.mainloop()


def main():
    global LANG, DEMO
    parser = argparse.ArgumentParser(description="Sessions Monitor for Claude Code")
    parser.add_argument("--lang", choices=sorted(STRINGS), help="interface language (default: Windows language)")
    parser.add_argument("--demo", action="store_true", help="fake sessions, nothing real is read or touched")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    args = parser.parse_args()
    LANG = args.lang or os.environ.get("CLAUDE_MONITOR_LANG") or LANG
    if LANG not in STRINGS:
        LANG = "en"
    DEMO = args.demo
    App().run()


if __name__ == "__main__":
    main()
