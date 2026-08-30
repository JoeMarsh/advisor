from __future__ import annotations

import ctypes
import os
import signal
import subprocess
from ctypes import wintypes
from typing import Any


def advisor_process_group_kwargs() -> dict[str, Any]:
    if os.name == "nt":
        return {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW}
    return {"start_new_session": True}


def advisor_detached_process_kwargs() -> dict[str, Any]:
    if os.name == "nt":
        return {
            "creationflags": (
                subprocess.CREATE_NEW_PROCESS_GROUP
                | subprocess.CREATE_NO_WINDOW
                | subprocess.DETACHED_PROCESS
            )
        }
    return {"start_new_session": True}


def process_is_running(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        kernel32 = ctypes.windll.kernel32
        kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel32.OpenProcess.restype = wintypes.HANDLE
        handle = kernel32.OpenProcess(0x1000, False, pid)
        if not handle:
            return False
        try:
            exit_code = wintypes.DWORD()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
                return False
            return exit_code.value == 259
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except (OSError, ProcessLookupError):
        return False
    return True


def terminate_process_tree(process: subprocess.Popen[Any]) -> None:
    if process.poll() is not None:
        return
    if os.name == "nt":
        for pid in windows_descendant_pids(process.pid):
            terminate_windows_process(pid)
        terminate_windows_process(process.pid)
    else:
        try:
            os.killpg(os.getpgid(process.pid), signal.SIGKILL)
        except (OSError, ProcessLookupError):
            process.kill()
    try:
        process.wait(timeout=15)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)


def terminate_pid_tree(pid: int) -> None:
    if not process_is_running(pid):
        return
    if os.name == "nt":
        for child_pid in windows_descendant_pids(pid):
            terminate_windows_process(child_pid)
        terminate_windows_process(pid)
        return
    try:
        os.killpg(os.getpgid(pid), signal.SIGKILL)
    except (OSError, ProcessLookupError):
        try:
            os.kill(pid, signal.SIGKILL)
        except (OSError, ProcessLookupError):
            pass


def windows_descendant_pids(root_pid: int) -> list[int]:
    if os.name != "nt":
        return []

    class ProcessEntry32(ctypes.Structure):
        _fields_ = [
            ("dwSize", wintypes.DWORD),
            ("cntUsage", wintypes.DWORD),
            ("th32ProcessID", wintypes.DWORD),
            ("th32DefaultHeapID", ctypes.c_size_t),
            ("th32ModuleID", wintypes.DWORD),
            ("cntThreads", wintypes.DWORD),
            ("th32ParentProcessID", wintypes.DWORD),
            ("pcPriClassBase", wintypes.LONG),
            ("dwFlags", wintypes.DWORD),
            ("szExeFile", wintypes.WCHAR * 260),
        ]

    kernel32 = ctypes.windll.kernel32
    kernel32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    kernel32.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(ProcessEntry32)]
    kernel32.Process32FirstW.restype = wintypes.BOOL
    kernel32.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(ProcessEntry32)]
    kernel32.Process32NextW.restype = wintypes.BOOL
    snapshot = kernel32.CreateToolhelp32Snapshot(0x00000002, 0)
    if snapshot == wintypes.HANDLE(-1).value:
        return []
    children: dict[int, list[int]] = {}
    try:
        entry = ProcessEntry32()
        entry.dwSize = ctypes.sizeof(entry)
        has_entry = bool(kernel32.Process32FirstW(snapshot, ctypes.byref(entry)))
        while has_entry:
            children.setdefault(int(entry.th32ParentProcessID), []).append(int(entry.th32ProcessID))
            has_entry = bool(kernel32.Process32NextW(snapshot, ctypes.byref(entry)))
    finally:
        kernel32.CloseHandle(snapshot)

    deepest_first: list[int] = []

    def visit(parent: int) -> None:
        for child in children.get(parent, []):
            visit(child)
            deepest_first.append(child)

    visit(root_pid)
    return deepest_first


def terminate_windows_process(pid: int) -> None:
    if os.name != "nt":
        return
    kernel32 = ctypes.windll.kernel32
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    handle = kernel32.OpenProcess(0x0001, False, pid)
    if not handle:
        return
    try:
        kernel32.TerminateProcess(handle, 1)
        kernel32.WaitForSingleObject(handle, 5000)
    finally:
        kernel32.CloseHandle(handle)
