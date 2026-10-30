"""Observe native process lifetimes and wait for exit without terminating saved PIDs."""
from __future__ import annotations
import ctypes,errno,os,platform,time
from pathlib import Path


class _ProcessEntry32W(ctypes.Structure):
    # Fixed-width Windows ABI, including pointer-sized ULONG_PTR. The name is
    # deliberately unused; snapshots never expose executable names to callers.
    _fields_ = [("dwSize", ctypes.c_uint32), ("cntUsage", ctypes.c_uint32),
        ("th32ProcessID", ctypes.c_uint32), ("th32DefaultHeapID", ctypes.c_size_t),
        ("th32ModuleID", ctypes.c_uint32), ("cntThreads", ctypes.c_uint32),
        ("th32ParentProcessID", ctypes.c_uint32), ("pcPriClassBase", ctypes.c_int32),
        ("dwFlags", ctypes.c_uint32), ("szExeFile", ctypes.c_uint16 * 260)]


_MAX_SNAPSHOT_PROCESSES = 131072


def _windows_error(stage, code=None):
    code = ctypes.get_last_error() if code is None else code
    error = OSError(errno.EACCES if code == 5 else errno.EIO,
        "Windows process observation failed at %s (WinError %s); exit is unconfirmed." % (stage, code))
    error.winerror = code
    error.observation_stage = stage
    return error


def _windows_process_present(kernel, pid):
    """An independent read-only snapshot distinguishes absence from denial.

    Only a complete process snapshot containing this observer can prove that a
    PID is absent. A listed PID, an empty/truncated list or any enumeration
    error cannot be treated as exit. No process is opened or terminated here.
    """
    from ctypes import wintypes as w
    kernel.CreateToolhelp32Snapshot.argtypes = [w.DWORD, w.DWORD]
    kernel.CreateToolhelp32Snapshot.restype = w.HANDLE
    kernel.Process32FirstW.argtypes = [w.HANDLE, ctypes.POINTER(_ProcessEntry32W)]
    kernel.Process32FirstW.restype = w.BOOL
    kernel.Process32NextW.argtypes = [w.HANDLE, ctypes.POINTER(_ProcessEntry32W)]
    kernel.Process32NextW.restype = w.BOOL
    kernel.CloseHandle.argtypes = [w.HANDLE]
    kernel.CloseHandle.restype = w.BOOL
    snapshot = kernel.CreateToolhelp32Snapshot(0x00000002, 0)  # TH32CS_SNAPPROCESS only
    if snapshot in (None, 0, -1, ctypes.c_void_p(-1).value):
        raise _windows_error("CreateToolhelp32Snapshot")
    try:
        entry = _ProcessEntry32W()
        entry.dwSize = ctypes.sizeof(entry)
        if not kernel.Process32FirstW(snapshot, ctypes.byref(entry)):
            raise _windows_error("Process32FirstW")
        saw_observer = False
        for _ in range(_MAX_SNAPSHOT_PROCESSES):
            if entry.th32ProcessID == pid:
                return True
            saw_observer = saw_observer or entry.th32ProcessID == os.getpid()
            if not kernel.Process32NextW(snapshot, ctypes.byref(entry)):
                code = ctypes.get_last_error()
                if code != 18:  # ERROR_NO_MORE_FILES is the only normal end.
                    raise _windows_error("Process32NextW", code)
                if not saw_observer:
                    raise _windows_error("ProcessSnapshotIncomplete", code)
                return False
        raise _windows_error("ProcessSnapshotLimit", 0)
    finally:
        kernel.CloseHandle(snapshot)


def os_process(pid, expected_identity=None):
    """Read process lifetime facts. This function has no terminate capability."""
    if type(pid) is not int or pid <= 0:
        raise ValueError("Invalid owned process identity.")
    if platform.system() == "Windows":
        if pid > 0xffffffff:
            raise ValueError("Invalid Windows process identifier.")
        from ctypes import wintypes as w
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.argtypes = [w.DWORD, w.BOOL, w.DWORD]
        kernel.OpenProcess.restype = w.HANDLE
        kernel.GetProcessTimes.argtypes = [w.HANDLE, ctypes.POINTER(w.FILETIME), ctypes.POINTER(w.FILETIME), ctypes.POINTER(w.FILETIME), ctypes.POINTER(w.FILETIME)]
        kernel.GetProcessTimes.restype = w.BOOL
        kernel.WaitForSingleObject.argtypes = [w.HANDLE, w.DWORD]
        kernel.WaitForSingleObject.restype = w.DWORD
        kernel.GetExitCodeProcess.argtypes = [w.HANDLE, ctypes.POINTER(w.DWORD)]
        kernel.GetExitCodeProcess.restype = w.BOOL
        kernel.CloseHandle.argtypes = [w.HANDLE]
        kernel.CloseHandle.restype = w.BOOL
        handle = kernel.OpenProcess(0x1000 | 0x100000, False, pid)
        if not handle:
            code = ctypes.get_last_error()
            if code == 87:
                return {"exists": False, "alive": False, "identity": None, "exit_code": None, "method": "WINDOWS_PROCESS_ABSENT"}
            # Access denial does not prove exit. A complete independent snapshot
            # may prove absence after shutdown; a present PID remains unknown.
            if code == 5 and not _windows_process_present(kernel, pid):
                return {"exists": False, "alive": False, "identity": None, "exit_code": None, "method": "WINDOWS_PROCESS_SNAPSHOT_ABSENT"}
            raise _windows_error("OpenProcess", code)
        try:
            created, exited, system, user = w.FILETIME(), w.FILETIME(), w.FILETIME(), w.FILETIME()
            if not kernel.GetProcessTimes(handle, ctypes.byref(created), ctypes.byref(exited), ctypes.byref(system), ctypes.byref(user)):
                raise _windows_error("GetProcessTimes")
            wait = kernel.WaitForSingleObject(handle, 0)
            if wait not in (0, 258):
                raise _windows_error("WaitForSingleObject")
            code = w.DWORD()
            if not kernel.GetExitCodeProcess(handle, ctypes.byref(code)):
                raise _windows_error("GetExitCodeProcess")
            return {"exists": True, "alive": wait == 258,
                "identity": {"platform": "Windows", "created_filetime": (created.dwHighDateTime << 32) | created.dwLowDateTime},
                "exit_code": None if wait == 258 else code.value, "method": "WINDOWS_PROCESS_HANDLE_SIGNAL"}
        finally:
            kernel.CloseHandle(handle)
    if platform.system() != "Linux":
        raise OSError("This bounded adapter requires Linux or Windows process identity support.")
    try:
        # procfs can be mounted from another PID namespace. The worker saves
        # its procfs PID from /proc/self instead of assuming getpid() names the
        # same process in that mount. Later observers use that saved lifetime.
        proc_pid = (expected_identity or {}).get("proc_pid", pid)
        path = Path("/proc/self/stat") if pid == os.getpid() and expected_identity is None else Path("/proc/%s/stat" % proc_pid)
        try:
            raw = path.read_text()
        except ProcessLookupError:
            # Linux may report ESRCH after opening procfs if the process exits
            # before the read. This proves absence; other read errors propagate.
            return {"exists": False, "alive": False, "identity": None, "exit_code": None, "method": "LINUX_PROCESS_ABSENT"}
        fields = raw.rpartition(")")[2].split()
        identity = {"platform": "Linux", "boot_id": Path("/proc/sys/kernel/random/boot_id").read_text().strip(),
                    "proc_pid": int(raw.split("(",1)[0]), "start_ticks": int(fields[19])}
        return {"exists": True, "alive": fields[0] not in {"Z", "X"}, "identity": identity,
                "exit_code": None, "method": "LINUX_PROCESS_START_AND_STATE"}
    except FileNotFoundError:
        return {"exists": False, "alive": False, "identity": None, "exit_code": None, "method": "LINUX_PROCESS_ABSENT"}


def wait_for_exit(pid,identity,timeout=15,observer=None):
    """Confirm the original lifetime ended; PID reuse is not an owned process.

    Only a Windows OpenProcess access denial is re-observed within the original
    deadline. Persistent/other observation errors propagate. A stop acknowledgement
    or a saved teardown marker alone cannot prove process exit/file release.
    """
    if type(pid) is not int or pid<=0 or not isinstance(identity,dict) or not identity:
        raise ValueError('A registered native process lifetime is required.')
    if not isinstance(timeout,(int,float)) or not 0<=timeout<=30:raise ValueError('Use a bounded native exit wait.')
    observer=observer or os_process;deadline=time.monotonic()+timeout
    while True:
        try:
            facts=observer(pid,identity)
        except OSError as error:
            remaining=deadline-time.monotonic()
            if (platform.system() != 'Windows' or getattr(error,'winerror',None) != 5
                    or getattr(error,'observation_stage',None) != 'OpenProcess' or remaining <= 0):
                raise
            # Retry only this read of the same registered lifetime. Never resend
            # shutdown, effects or a provider request, and never renew a lease.
            time.sleep(min(.025,remaining))
            continue
        if not facts['exists'] or facts['identity']!=identity or facts['alive'] is False:return True
        if time.monotonic()>=deadline:return False
        time.sleep(.025)
