"""
Unattended benchmark queue.   python -m benchmarks.overnight

Runs the live suites one after another, each in its own process, and keeps the machine and the queue healthy:
  * keeps Windows awake while it runs (no sleep mid-suite);
  * runs each suite at below-normal priority, waits for free RAM before starting it, and kills a suite that
    grows past MAX_RSS_MB;
  * kills and restarts a suite whose log has not moved for STALL_MINUTES (a hung network call); the harness caches
    every model response, so a restart resumes where it stopped;
  * stops retrying a suite once the provider's daily quota is used up (retrying cannot help) and moves on;
  * never starts a new suite after DEADLINE_HOURS.

Progress for a human (or another tool) to read:  logs/overnight/status.json  and  logs/overnight/<suite>.log
"""

import ctypes
import json
import os
import subprocess
import sys
import time
from ctypes import wintypes
from datetime import datetime

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
LOG_DIR = os.path.join(ROOT, "logs", "overnight")
STATUS = os.path.join(LOG_DIR, "status.json")

# (suite, extra environment, hard time limit in hours for all attempts of this suite)
QUEUE = [
    ("ablation", {}, 3.5),
    ("latency", {}, 1.0),
    ("security", {}, 2.0),
    ("retrieval", {"RETRIEVAL_POOLS": "all"}, 2.0),
]
DEADLINE_HOURS = 5.75
MAX_ATTEMPTS = 3
MAX_RSS_MB = 6000
MIN_FREE_MB = 3000
STALL_MINUTES = 25
POLL_SECONDS = 20
QUOTA_MARKERS = ("DailyQuotaExceeded", "paused: DailyQuota", "paused: EmbeddingUnavailable", "RESOURCE_EXHAUSTED")

BELOW_NORMAL = 0x00004000
ES_CONTINUOUS, ES_SYSTEM_REQUIRED = 0x80000000, 0x00000001


def free_mb() -> int:
    class MemStatus(ctypes.Structure):
        _fields_ = [("length", ctypes.c_ulong), ("load", ctypes.c_ulong), ("total", ctypes.c_ulonglong), ("avail", ctypes.c_ulonglong),
                    ("tpf", ctypes.c_ulonglong), ("apf", ctypes.c_ulonglong), ("tv", ctypes.c_ulonglong), ("av", ctypes.c_ulonglong),
                    ("ave", ctypes.c_ulonglong)]
    m = MemStatus()
    m.length = ctypes.sizeof(MemStatus)
    ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(m))
    return int(m.avail / 1048576)


class _ProcessEntry(ctypes.Structure):
    _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD), ("th32ProcessID", wintypes.DWORD),
                ("th32DefaultHeapID", ctypes.c_size_t), ("th32ModuleID", wintypes.DWORD), ("cntThreads", wintypes.DWORD),
                ("th32ParentProcessID", wintypes.DWORD), ("pcPriClassBase", ctypes.c_long), ("dwFlags", wintypes.DWORD),
                ("szExeFile", ctypes.c_char * 260)]


class _MemCounters(ctypes.Structure):
    _fields_ = [("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD), ("PeakWorkingSetSize", ctypes.c_size_t),
                ("WorkingSetSize", ctypes.c_size_t), ("QuotaPeakPagedPoolUsage", ctypes.c_size_t), ("QuotaPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t), ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                ("PagefileUsage", ctypes.c_size_t), ("PeakPagefileUsage", ctypes.c_size_t)]


def _process_tree(root_pid: int) -> list:
    """root_pid and every descendant. The venv's python.exe is a launcher: the interpreter doing the work is its child."""
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    k32.Process32First.argtypes = k32.Process32Next.argtypes = [wintypes.HANDLE, ctypes.POINTER(_ProcessEntry)]
    k32.CloseHandle.argtypes = [wintypes.HANDLE]
    snapshot = k32.CreateToolhelp32Snapshot(0x00000002, 0)          # TH32CS_SNAPPROCESS
    children = {}
    entry = _ProcessEntry()
    entry.dwSize = ctypes.sizeof(_ProcessEntry)
    try:
        more = k32.Process32First(snapshot, ctypes.byref(entry))
        while more:
            children.setdefault(entry.th32ParentProcessID, []).append(entry.th32ProcessID)
            more = k32.Process32Next(snapshot, ctypes.byref(entry))
    finally:
        k32.CloseHandle(snapshot)
    tree, queue = [], [root_pid]
    while queue:
        pid = queue.pop()
        if pid not in tree:
            tree.append(pid)
            queue.extend(children.get(pid, []))
    return tree


def rss_mb(pid: int) -> int:
    """Working-set memory of the process and all its descendants, in MB (0 if it cannot be read)."""
    try:
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.OpenProcess.restype = wintypes.HANDLE
        k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        k32.K32GetProcessMemoryInfo.argtypes = [wintypes.HANDLE, ctypes.POINTER(_MemCounters), wintypes.DWORD]
        k32.CloseHandle.argtypes = [wintypes.HANDLE]
        total = 0
        for p in _process_tree(pid):
            handle = k32.OpenProcess(0x1000, False, p)              # PROCESS_QUERY_LIMITED_INFORMATION
            if not handle:
                continue
            try:
                counters = _MemCounters()
                counters.cb = ctypes.sizeof(_MemCounters)
                if k32.K32GetProcessMemoryInfo(handle, ctypes.byref(counters), counters.cb):
                    total += counters.WorkingSetSize
            finally:
                k32.CloseHandle(handle)
        return int(total / 1048576)
    except Exception:
        return 0


def kill_tree(proc: subprocess.Popen) -> None:
    subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"], capture_output=True)


def tail(path: str, n: int = 3) -> str:
    try:
        with open(path, "rb") as f:
            f.seek(max(0, os.path.getsize(path) - 4000))
            return " | ".join(f.read().decode("utf-8", "replace").strip().splitlines()[-n:])
    except OSError:
        return ""


def now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


state = {"started": now(), "suites": {}, "current": None, "note": "", "free_mb": 0, "done": False}


def write_status(**kw) -> None:
    state.update(kw)
    state["updated"] = now()
    state["free_mb"] = free_mb()
    tmp = STATUS + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=1)
    os.replace(tmp, STATUS)


def run_suite(name: str, extra_env: dict, limit_hours: float, global_deadline: float) -> str:
    log = os.path.join(LOG_DIR, f"{name}.log")
    suite_deadline = min(time.time() + limit_hours * 3600, global_deadline)
    result = "failed"
    for attempt in range(1, MAX_ATTEMPTS + 1):
        waited = 0
        while free_mb() < MIN_FREE_MB and waited < 1800:       # let the machine breathe before starting
            write_status(note=f"{name}: waiting for RAM ({free_mb()} MB free)")
            time.sleep(30)
            waited += 30
        env = dict(os.environ, PYTHONUNBUFFERED="1", PYTHONIOENCODING="utf-8", **extra_env)
        with open(log, "a", encoding="utf-8", errors="replace") as lf:
            lf.write(f"\n##### {now()} attempt {attempt} of {name}\n")
        lf = open(log, "ab")
        proc = subprocess.Popen([sys.executable, "-u", "-m", "benchmarks.run", name], cwd=ROOT, env=env, stdout=lf, stderr=subprocess.STDOUT,
                                creationflags=BELOW_NORMAL)
        state["suites"][name] = {"status": "running", "attempt": attempt, "started": now()}
        last_size, last_move, why = -1, time.time(), None
        while proc.poll() is None:
            time.sleep(POLL_SECONDS)
            size = os.path.getsize(log)
            if size != last_size:
                last_size, last_move = size, time.time()
            rss = rss_mb(proc.pid)
            state["suites"][name].update({"rss_mb": rss, "log_tail": tail(log, 2)[-300:]})
            write_status(current=name, note="")
            if rss > MAX_RSS_MB:
                why = f"memory {rss} MB over limit"
            elif time.time() - last_move > STALL_MINUTES * 60:
                why = f"no log output for {STALL_MINUTES} minutes"
            elif time.time() > suite_deadline:
                why = "time limit reached"
            if why:
                kill_tree(proc)
                break
        lf.close()
        code = proc.wait()
        text = ""
        try:
            with open(log, "r", encoding="utf-8", errors="replace") as f:
                text = f.read()[-20000:]
        except OSError:
            pass
        # the quota check looks only at this attempt's output
        attempt_text = text.split(f"attempt {attempt} of {name}")[-1]
        if why == "time limit reached":
            result = "time-limit"
            break
        if why is None and code == 0:
            paused = any(m in attempt_text for m in QUOTA_MARKERS) or "status\": \"paused" in attempt_text
            result = "paused-quota (resume tomorrow)" if paused else "done"
            break
        if any(m in attempt_text for m in QUOTA_MARKERS):
            result = "paused-quota (resume tomorrow)"
            break
        result = f"failed ({why or 'exit ' + str(code)})"
        write_status(note=f"{name} attempt {attempt}: {result}; retrying in 90 s")
        time.sleep(90)
        if time.time() > suite_deadline:
            break
    return result


def main() -> int:
    os.makedirs(LOG_DIR, exist_ok=True)
    ctypes.windll.kernel32.SetThreadExecutionState(ES_CONTINUOUS | ES_SYSTEM_REQUIRED)
    global_deadline = time.time() + DEADLINE_HOURS * 3600
    write_status(note="started")
    for name, extra_env, hours in QUEUE:
        if time.time() > global_deadline:
            state["suites"][name] = {"status": "not started (deadline)"}
            continue
        result = run_suite(name, extra_env, hours, global_deadline)
        state["suites"][name] = {"status": result, "finished": now(), "log_tail": tail(os.path.join(LOG_DIR, f"{name}.log"), 3)[-400:]}
        write_status(current=None, note=f"{name}: {result}")
    write_status(current=None, done=True, note="queue finished")
    ctypes.windll.kernel32.SetThreadExecutionState(ES_CONTINUOUS)
    return 0


if __name__ == "__main__":
    sys.exit(main())
