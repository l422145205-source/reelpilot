"""Bounded pipe capture and ownership-scoped process-tree termination.

Windows uses an unnamed kill-on-close Job Object. POSIX uses a new session.
This is lifetime management for trusted tools, not a security sandbox.
"""

from __future__ import annotations

from collections import deque
import os
import signal
import subprocess
from threading import Lock, Thread
import time

from .control import ProcessCleanupError

INSIDE_WORKER = False


class WindowsJob:
    def __init__(self, pid):
        import ctypes as c
        from ctypes import wintypes as w

        class Basic(c.Structure):
            _fields_ = [
                ("process_time", c.c_longlong),
                ("job_time", c.c_longlong),
                ("flags", w.DWORD),
                ("min_ws", c.c_size_t),
                ("max_ws", c.c_size_t),
                ("active", w.DWORD),
                ("affinity", c.c_size_t),
                ("priority", w.DWORD),
                ("scheduling", w.DWORD),
            ]

        class IO(c.Structure):
            _fields_ = [
                (name, c.c_ulonglong)
                for name in ("read_ops", "write_ops", "other_ops", "read", "write", "other")
            ]

        class Extended(c.Structure):
            _fields_ = [
                ("basic", Basic),
                ("io", IO),
                ("process_mem", c.c_size_t),
                ("job_mem", c.c_size_t),
                ("peak_process", c.c_size_t),
                ("peak_job", c.c_size_t),
            ]

        k = c.WinDLL("kernel32", use_last_error=True)
        k.CreateJobObjectW.argtypes = [c.c_void_p, w.LPCWSTR]
        k.CreateJobObjectW.restype = w.HANDLE
        k.SetInformationJobObject.argtypes = [w.HANDLE, c.c_int, c.c_void_p, w.DWORD]
        k.SetInformationJobObject.restype = w.BOOL
        k.OpenProcess.argtypes = [w.DWORD, w.BOOL, w.DWORD]
        k.OpenProcess.restype = w.HANDLE
        k.AssignProcessToJobObject.argtypes = [w.HANDLE, w.HANDLE]
        k.AssignProcessToJobObject.restype = w.BOOL
        k.TerminateJobObject.argtypes = [w.HANDLE, w.UINT]
        k.TerminateJobObject.restype = w.BOOL
        k.QueryInformationJobObject.argtypes = [w.HANDLE, c.c_int, c.c_void_p, w.DWORD, c.c_void_p]
        k.QueryInformationJobObject.restype = w.BOOL
        k.CloseHandle.argtypes = [w.HANDLE]
        k.CloseHandle.restype = w.BOOL
        self.k, self.handle = k, k.CreateJobObjectW(None, None)
        if not self.handle:
            raise c.WinError(c.get_last_error())
        process = None
        try:
            limits = Extended()
            limits.basic.flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            if not k.SetInformationJobObject(self.handle, 9, c.byref(limits), c.sizeof(limits)):
                raise c.WinError(c.get_last_error())
            process = k.OpenProcess(0x0100 | 0x0001, False, pid)  # SET_QUOTA | TERMINATE, own child only
            if not process or not k.AssignProcessToJobObject(self.handle, process):
                raise c.WinError(c.get_last_error())
        except BaseException:
            self.close()
            raise
        finally:
            if process:
                k.CloseHandle(process)

    def close(self):
        if self.handle:
            self.k.CloseHandle(self.handle)
            self.handle = None

    def kill_and_wait(self, timeout=5):
        """Acknowledge explicit termination only after the job has no active process."""
        import ctypes as c
        from ctypes import wintypes as w

        class Accounting(c.Structure):
            _fields_ = [
                (name, c.c_longlong) for name in ("user", "kernel", "period_user", "period_kernel")
            ] + [("faults", w.DWORD), ("total", w.DWORD), ("active", w.DWORD), ("terminated", w.DWORD)]

        try:
            if not self.k.TerminateJobObject(self.handle, 1):
                raise c.WinError(c.get_last_error())
            until = time.monotonic() + timeout
            while True:
                info = Accounting()
                if not self.k.QueryInformationJobObject(self.handle, 1, c.byref(info), c.sizeof(info), None):
                    raise c.WinError(c.get_last_error())
                if info.active == 0:
                    return
                if time.monotonic() >= until:
                    raise TimeoutError("owned Windows job did not terminate")
                time.sleep(0.01)
        finally:
            self.close()  # Kill-on-close remains the fallback, including parent death.


class Tail:
    def __init__(self, pipe, limit=65536):
        self.limit, self.total, self.size = limit, 0, 0
        self.parts, self.lock = deque(), Lock()

        def drain():
            try:
                while chunk := pipe.read(4096):
                    with self.lock:
                        self.total += len(chunk)
                        self.parts.append(chunk)
                        self.size += len(chunk)
                        while self.size > self.limit:
                            excess = self.size - self.limit
                            first = self.parts.popleft()
                            if len(first) > excess:
                                self.parts.appendleft(first[excess:])
                                self.size -= excess
                            else:
                                self.size -= len(first)
            finally:
                pipe.close()

        self.thread = Thread(target=drain, name="reelpilot-pipe", daemon=True)
        self.thread.start()

    def value(self):
        with self.lock:
            return b"".join(self.parts).decode("utf-8", errors="replace")


class OwnedProcess:
    def __init__(self, command, *, gated=False, isolate=True, env=None):
        self.isolate, self.job = isolate, None
        self._terminated = self._closed = False
        self._termination_error = None
        options = (
            {"creationflags": subprocess.CREATE_NO_WINDOW}
            if os.name == "nt"
            else {"start_new_session": isolate}
        )
        self.process = subprocess.Popen(
            command,
            stdin=subprocess.PIPE if gated else subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            shell=False,
            env=env,
            **options,
        )
        try:
            if os.name == "nt" and isolate:
                self.job = WindowsJob(self.process.pid)
        except BaseException:
            self.process.kill()
            self.process.wait(timeout=5)
            self.process.stdout.close()
            self.process.stderr.close()
            if self.process.stdin:
                self.process.stdin.close()
            raise
        self.stdout, self.stderr = Tail(self.process.stdout), Tail(self.process.stderr)

    def send(self, payload):
        self.process.stdin.write(payload)
        self.process.stdin.flush()
        self.process.stdin.close()

    def terminate_tree(self):
        if self._terminated:
            if self._termination_error is not None:
                raise ProcessCleanupError(
                    "owned process termination was not confirmed"
                ) from self._termination_error
            return
        # Never signal the numeric process group twice: its ID may be reused.
        self._terminated = True
        try:
            if self.job is not None:
                try:
                    self.job.kill_and_wait()
                finally:
                    self.job = None
            elif os.name != "nt" and self.isolate:
                try:
                    os.killpg(self.process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            elif self.process.poll() is None:
                self.process.kill()
            self.process.wait(timeout=5)
        except Exception as exc:
            self._termination_error = exc
            raise ProcessCleanupError("owned process termination was not confirmed") from exc
        if self.process.stdin and not self.process.stdin.closed:
            try:
                self.process.stdin.close()
            except BrokenPipeError:
                pass

    def wait(self, *, check=None, timeout=180, interval=0.05):
        deadline = time.monotonic() + timeout
        try:
            while True:
                if check is not None:
                    check()
                if self.process.poll() is not None:
                    break
                if time.monotonic() >= deadline:
                    raise TimeoutError("tool process timeout")
                time.sleep(interval)
            return self.process.returncode
        except BaseException:
            self.terminate_tree()
            raise
        finally:
            self.close()

    def close(self):
        if self._closed:
            return
        # Also remove descendants which outlive a normally exiting group leader.
        self.terminate_tree()
        self.stdout.thread.join(timeout=2)
        self.stderr.thread.join(timeout=2)
        self._closed = True
