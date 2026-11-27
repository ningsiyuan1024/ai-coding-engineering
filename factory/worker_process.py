"""Lifetime of one native execution process tree owned by a Symphony worker."""

import ctypes
from ctypes import wintypes
import os
import signal
import subprocess
import threading
import time


def port_cancelled(fd=0):
    """Monitor an owned input pipe without holding Python's buffered stdin lock."""
    flag = threading.Event()

    def read():
        pending = b""
        try:
            while True:
                chunk = os.read(fd, 1024)
                if not chunk:
                    break
                pending += chunk
                if b"cancel" in pending.split():
                    break
                pending = pending[-16:]
        except OSError:
            pass
        flag.set()

    threading.Thread(target=read, daemon=True).start()
    return flag


class _BasicLimits(ctypes.Structure):
    _fields_ = [("process_time", ctypes.c_longlong), ("job_time", ctypes.c_longlong),
                ("flags", wintypes.DWORD), ("min_ws", ctypes.c_size_t), ("max_ws", ctypes.c_size_t),
                ("active_limit", wintypes.DWORD), ("affinity", ctypes.c_size_t),
                ("priority", wintypes.DWORD), ("scheduling", wintypes.DWORD)]


class _ExtendedLimits(ctypes.Structure):
    _fields_ = [("basic", _BasicLimits), ("io_counts", ctypes.c_ulonglong * 6),
                ("memory", ctypes.c_size_t * 4)]


class _Accounting(ctypes.Structure):
    _fields_ = [("times", ctypes.c_longlong * 4), ("faults", wintypes.DWORD),
                ("total", wintypes.DWORD), ("active", wintypes.DWORD), ("ended", wintypes.DWORD)]


def _kernel():
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    signatures = {
        "CreateJobObjectW": (wintypes.HANDLE, [ctypes.c_void_p, wintypes.LPCWSTR]),
        "OpenJobObjectW": (wintypes.HANDLE, [wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR]),
        "SetInformationJobObject": (wintypes.BOOL, [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]),
        "QueryInformationJobObject": (wintypes.BOOL, [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD, ctypes.c_void_p]),
        "AssignProcessToJobObject": (wintypes.BOOL, [wintypes.HANDLE, wintypes.HANDLE]),
        "TerminateJobObject": (wintypes.BOOL, [wintypes.HANDLE, wintypes.UINT]),
        "CloseHandle": (wintypes.BOOL, [wintypes.HANDLE]),
    }
    for name, (result, arguments) in signatures.items():
        function = getattr(kernel, name)
        function.argtypes, function.restype = arguments, result
    return kernel


def _check(success):
    if not success:
        raise ctypes.WinError(ctypes.get_last_error())
    return success


def _stop_job(kernel, handle):
    _check(kernel.TerminateJobObject(handle, 1))
    deadline = time.monotonic() + 10
    while True:
        accounting = _Accounting()
        _check(kernel.QueryInformationJobObject(handle, 1, ctypes.byref(accounting),
                                                ctypes.sizeof(accounting), None))
        if accounting.active == 0:
            return
        if time.monotonic() >= deadline:
            raise TimeoutError("Native workers did not terminate")
        time.sleep(0.025)


def orphan_gone(token, pid):
    """Verify a former guardian's tree is gone before recovering its exact DB run."""
    if os.name == "nt":
        kernel = _kernel()
        handle = kernel.OpenJobObjectW(0x0004 | 0x0008, False, "Local\\factory-symphony-" + token)
        if not handle:
            if ctypes.get_last_error() == 2:
                return True
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            _stop_job(kernel, handle)
            return True
        finally:
            kernel.CloseHandle(handle)
    try:
        os.killpg(pid, 0)
    except ProcessLookupError:
        return True
    # A persistent PID alone is insufficient proof for signalling a possibly reused group.
    return False


class ProcessTree:
    """The executor must wait for start on stdin before running any child process."""

    def __init__(self, argv, token, stdout, *, cwd):
        self.kernel = _kernel() if os.name == "nt" else None
        self.job = None
        self.process = None
        try:
            if self.kernel:
                self.job = _check(self.kernel.CreateJobObjectW(None, "Local\\factory-symphony-" + token))
                if ctypes.get_last_error() == 183:
                    raise RuntimeError("A worker job already exists for this run")
                limits = _ExtendedLimits()
                limits.basic.flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE, no breakaway.
                _check(self.kernel.SetInformationJobObject(self.job, 9, ctypes.byref(limits), ctypes.sizeof(limits)))
            self.process = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=stdout,
                                            stderr=subprocess.DEVNULL, cwd=cwd, close_fds=True,
                                            start_new_session=os.name != "nt",
                                            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
            if self.kernel:
                _check(self.kernel.AssignProcessToJobObject(self.job, int(self.process._handle)))
        except BaseException:
            if self.process:
                self.process.kill()
                self.process.wait()
                self.process.stdin.close()
            if self.job:
                self.kernel.CloseHandle(self.job)
            raise

    def start(self):
        self.process.stdin.write(b"start\n")
        self.process.stdin.flush()

    def close(self):
        try:
            if self.kernel:
                _stop_job(self.kernel, self.job)
            else:
                try:
                    os.killpg(self.process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            self.process.wait(timeout=10)
        finally:
            if self.job:
                self.kernel.CloseHandle(self.job)
                self.job = None
            self.process.stdin.close()


def wait_for_start():
    """A lost guardian also kills the executor's POSIX group after startup."""
    import sys
    if sys.stdin.buffer.readline(16) != b"start\n":
        return False
    if os.name != "nt":
        cancelled = port_cancelled()

        def lost_guardian():
            cancelled.wait()
            os.killpg(os.getpgrp(), signal.SIGKILL)

        threading.Thread(target=lost_guardian, daemon=True).start()
    return True
