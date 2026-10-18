"""Windows process ownership: birth time, executable and explicit launch token.

Keep the queried process handle open through termination to avoid PID-reuse races.
No process-tree termination and no fallback to an attached Windows Job spawn.
"""
from __future__ import annotations

import ctypes
from ctypes import wintypes as w
import json
import os
from pathlib import Path
import subprocess
import uuid


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def _kernel():
    k = ctypes.WinDLL("kernel32", use_last_error=True)
    signatures = {
        "OpenProcess": ([w.DWORD, w.BOOL, w.DWORD], w.HANDLE),
        "CloseHandle": ([w.HANDLE], w.BOOL),
        "GetCurrentProcess": ([], w.HANDLE),
        "IsProcessInJob": ([w.HANDLE, w.HANDLE, ctypes.POINTER(w.BOOL)], w.BOOL),
        "GetProcessTimes": ([w.HANDLE] + [ctypes.POINTER(w.FILETIME)] * 4, w.BOOL),
        "QueryFullProcessImageNameW": ([w.HANDLE, w.DWORD, w.LPWSTR, ctypes.POINTER(w.DWORD)], w.BOOL),
        "GetExitCodeProcess": ([w.HANDLE, ctypes.POINTER(w.DWORD)], w.BOOL),
        "TerminateProcess": ([w.HANDLE, w.UINT], w.BOOL),
        "WaitForSingleObject": ([w.HANDLE, w.DWORD], w.DWORD),
    }
    for name, (arguments, result) in signatures.items():
        getattr(k, name).argtypes = arguments
        getattr(k, name).restype = result
    return k


def detached_flags():
    if os.name != "nt":
        raise OSError("此启动器需要 Windows")
    k = _kernel(); member = w.BOOL()
    if not k.IsProcessInJob(k.GetCurrentProcess(), None, ctypes.byref(member)):
        raise ctypes.WinError(ctypes.get_last_error())
    return subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP | (0x01000000 if member.value else 0)


def _inspect(k, handle, pid):
    exit_code = w.DWORD()
    if not k.GetExitCodeProcess(handle, ctypes.byref(exit_code)) or exit_code.value != 259:
        return None
    times = [w.FILETIME() for _ in range(4)]
    if not k.GetProcessTimes(handle, *(ctypes.byref(t) for t in times)):
        raise ctypes.WinError(ctypes.get_last_error())
    image = ctypes.create_unicode_buffer(32768); size = w.DWORD(len(image))
    if not k.QueryFullProcessImageNameW(handle, 0, image, ctypes.byref(size)):
        raise ctypes.WinError(ctypes.get_last_error())
    class UnicodeString(ctypes.Structure):
        _fields_ = [("Length", w.USHORT), ("MaximumLength", w.USHORT), ("Buffer", ctypes.c_void_p)]
    nt = ctypes.WinDLL("ntdll").NtQueryInformationProcess
    nt.argtypes = [w.HANDLE, w.ULONG, ctypes.c_void_p, w.ULONG, ctypes.POINTER(w.ULONG)]
    nt.restype = w.LONG
    needed = w.ULONG()
    nt(handle, 60, None, 0, ctypes.byref(needed))
    if not 0 < needed.value <= 131072:
        raise OSError("无法核验进程命令行")
    buffer = ctypes.create_string_buffer(needed.value)
    if nt(handle, 60, buffer, len(buffer), ctypes.byref(needed)) < 0:
        raise OSError("无法读取进程命令行")
    string = UnicodeString.from_buffer(buffer)
    command = ctypes.wstring_at(string.Buffer, string.Length // 2)
    shell = ctypes.WinDLL("shell32")
    shell.CommandLineToArgvW.argtypes = [w.LPCWSTR, ctypes.POINTER(ctypes.c_int)]
    shell.CommandLineToArgvW.restype = ctypes.POINTER(w.LPWSTR)
    count = ctypes.c_int(); argv = shell.CommandLineToArgvW(command, ctypes.byref(count))
    if not argv:
        raise OSError("无法解析进程命令行")
    try:
        arguments = [argv[i] for i in range(count.value)]
    finally:
        k.LocalFree.argtypes = [ctypes.c_void_p]; k.LocalFree.restype = ctypes.c_void_p
        k.LocalFree(argv)
    return {"pid": int(pid), "created": (times[0].dwHighDateTime << 32) | times[0].dwLowDateTime,
            "executable": os.path.normcase(os.path.realpath(image.value)), "argv": arguments}


def identity(pid):
    if not pid or os.name != "nt":
        return None
    k = _kernel(); handle = k.OpenProcess(0x1000, False, int(pid))
    if not handle:
        return None
    try:
        return _inspect(k, handle, pid)
    except OSError:
        return None
    finally:
        k.CloseHandle(handle)


def wait_identity_exit(expected, timeout=60):
    """Wait for one captured process identity; never request termination rights.

    A recycled PID means the captured process has exited. Callers must rescan
    their installation before replacing files, to reject a newly started peer.
    """
    if (not isinstance(expected, dict) or not isinstance(expected.get('pid'), int)
            or expected['pid'] <= 0 or not isinstance(expected.get('created'), int)
            or not isinstance(expected.get('executable'), str)
            or not isinstance(expected.get('argv'), list)):
        raise ValueError('等待退出需要已核验的完整进程身份')
    timeout = float(timeout)
    if not 0 <= timeout <= 600:
        raise ValueError('退出等待期限无效')
    k = _kernel()
    # PROCESS_QUERY_LIMITED_INFORMATION | SYNCHRONIZE; no PROCESS_TERMINATE.
    handle = k.OpenProcess(0x1000 | 0x100000, False, expected['pid'])
    if not handle:
        error = ctypes.get_last_error()
        if error in (0, 87, 1168):
            return True
        raise ctypes.WinError(error)
    try:
        actual = _inspect(k, handle, expected['pid'])
        if actual is None:
            return True
        if actual != {key: expected.get(key) for key in actual}:
            return True
        result = k.WaitForSingleObject(handle, int(timeout * 1000))
        if result == 0:
            return True
        if result == 258:
            return False
        raise ctypes.WinError(ctypes.get_last_error())
    finally:
        k.CloseHandle(handle)


def _process_ids_named(name):
    class Entry(ctypes.Structure):
        _fields_ = [("dwSize", w.DWORD), ("cntUsage", w.DWORD), ("th32ProcessID", w.DWORD),
                    ("th32DefaultHeapID", ctypes.c_size_t), ("th32ModuleID", w.DWORD),
                    ("cntThreads", w.DWORD), ("th32ParentProcessID", w.DWORD),
                    ("pcPriClassBase", w.LONG), ("dwFlags", w.DWORD), ("szExeFile", w.WCHAR * 260)]
    k = _kernel()
    k.CreateToolhelp32Snapshot.argtypes = [w.DWORD, w.DWORD]
    k.CreateToolhelp32Snapshot.restype = w.HANDLE
    for method in ('Process32FirstW', 'Process32NextW'):
        getattr(k, method).argtypes = [w.HANDLE, ctypes.POINTER(Entry)]
        getattr(k, method).restype = w.BOOL
    snapshot = k.CreateToolhelp32Snapshot(2, 0)
    if snapshot == ctypes.c_void_p(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    result = []
    try:
        entry = Entry(); entry.dwSize = ctypes.sizeof(entry)
        present = k.Process32FirstW(snapshot, ctypes.byref(entry))
        while present:
            if entry.szExeFile.casefold() == name.casefold():
                result.append(int(entry.th32ProcessID))
            present = k.Process32NextW(snapshot, ctypes.byref(entry))
    finally:
        k.CloseHandle(snapshot)
    return result


def image_processes(executable):
    """Read identities for exactly one executable path; no process is changed."""
    target = os.path.normcase(os.path.realpath(executable))
    result = []
    for pid in _process_ids_named(Path(executable).name):
        record = identity(pid)
        if record is None:
            # A process may end between enumeration and inspection. A live
            # same-name process whose identity is unreadable is not safe proof.
            k = _kernel(); handle = k.OpenProcess(0x100000, False, pid)
            if not handle:
                if ctypes.get_last_error() not in (0,87,1168):
                    raise OSError('无法核验同名原生进程，已拒绝更新')
                continue
            try:
                if k.WaitForSingleObject(handle,0) != 0:
                    raise OSError('无法核验仍在运行的同名原生进程，已拒绝更新')
            finally:
                k.CloseHandle(handle)
        if record and record['executable'] == target:
            result.append(record)
    return sorted(result, key=lambda record: (record['pid'], record['created']))


def in_job(pid):
    k = _kernel(); handle = k.OpenProcess(0x1000, False, int(pid))
    if not handle: raise ctypes.WinError(ctypes.get_last_error())
    try:
        member = w.BOOL()
        if not k.IsProcessInJob(handle, None, ctypes.byref(member)):
            raise ctypes.WinError(ctypes.get_last_error())
        return bool(member.value)
    finally:
        k.CloseHandle(handle)


def matches(actual, expected, allowed_images):
    if not actual or not expected: return False
    prefix = ["-m", "knowledge_bridge.cli", expected.get("role"), "--launch-id", expected.get("launch_id")]
    arguments = actual["argv"][1:]
    # New installations keep their immutable program/runtime inventory clean.
    # Retain identity checks for already-running releases without the -B flag.
    if arguments[:1] == ["-B"]: arguments = arguments[1:]
    suffix = arguments[5:]
    if len(suffix)>=2 and suffix[-2]=='--test-owner-seconds' and suffix[-1].isdigit(): suffix=suffix[:-2]
    if suffix and suffix[-1] == "--isolated-test": suffix = suffix[:-1]
    runtime=[]
    if len(suffix)>=6 and suffix[-6]=='--model-precision' and suffix[-4]=='--embedding-batch-size' and suffix[-2]=='--embedding-duty-cycle':
        runtime=suffix[-6:];suffix=suffix[:-6]
    try:duty_valid=.1<=float(runtime[5])<=1 if runtime else True
    except ValueError:duty_valid=False
    valid_runtime=(not runtime or (runtime[1] in {'fp32','fp16'} and runtime[3].isdigit() and 1<=int(runtime[3])<=128 and duty_valid))
    valid_suffix = (not suffix or (len(suffix) == 4 and suffix[0] == "--vault" and suffix[2] == "--local-data")) and valid_runtime
    return bool(actual == {key: expected.get(key) for key in actual}
                and actual["executable"] in allowed_images and arguments[:5] == prefix
                and valid_suffix and expected.get("role") in {"console", "watch"} and expected.get("launch_id"))


def spawn_service(executable, bridge_dir, role, token, vault, local_data, *, model_precision=None,embedding_batch_size=None,embedding_duty_cycle=1.0):
    """Use the existing Windows WMI provider as a short-lived creation broker.

    CREATE_BREAKAWAY_FROM_JOB alone can leave a process in an outer nested Job.
    No scheduled task, service installation, credentials or persistent OS settings.
    Pass only paths and a non-secret launch token; the child opens its own logs.
    """
    arguments = [str(executable), "-B", "-m", "knowledge_bridge.cli", role,
                                      "--launch-id", token, "--vault", str(vault),
                                      "--local-data", str(local_data)]
    if model_precision:
        arguments.extend(['--model-precision',str(model_precision),'--embedding-batch-size',str(int(embedding_batch_size)),
                          '--embedding-duty-cycle',str(float(embedding_duty_cycle))])
    if os.environ.get("KNOWLEDGE_BRIDGE_PROTECTION_TEST_MODE") == "1":
        arguments.append("--isolated-test")
        if os.environ.get('KNOWLEDGE_BRIDGE_OWNER_SESSION_SECONDS'):
            arguments.extend(['--test-owner-seconds',str(int(os.environ['KNOWLEDGE_BRIDGE_OWNER_SESSION_SECONDS']))])
    command = subprocess.list2cmdline(arguments)
    def quote(value): return "'" + str(value).replace("'", "''") + "'"
    script = ("$ErrorActionPreference='Stop'; "
              "$taskStartup=New-CimInstance -ClassName Win32_ProcessStartup -ClientOnly "
              "-Property @{ShowWindow=[uint16]0;CreateFlags=[uint32]16777224}; "
              "$taskResult=Invoke-CimMethod -ClassName Win32_Process -MethodName Create -Arguments "
              "@{CommandLine=" + quote(command) + ";CurrentDirectory=" + quote(bridge_dir) +
              ";ProcessStartupInformation=$taskStartup}; "
              "$taskResult | Select-Object ReturnValue,ProcessId | ConvertTo-Json -Compress")
    powershell = Path(os.environ["SystemRoot"]) / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"
    result = subprocess.run([str(powershell), "-NoProfile", "-NonInteractive", "-Command", script],
                            stdin=subprocess.DEVNULL, capture_output=True, timeout=30,
                            creationflags=subprocess.CREATE_NO_WINDOW)
    if result.returncode:
        raise OSError("Windows独立进程创建接口失败；请查看WMI/CIM可用性，未回退到宿主附属启动")
    response = json.loads(result.stdout)
    if response.get("ReturnValue") != 0:
        raise OSError(f"Windows独立启动失败，返回码 {response.get('ReturnValue')}；未回退到宿主附属启动")
    return int(response["ProcessId"])


def terminate_owned(expected, allowed_images):
    if not expected or not expected.get("pid"):
        return False
    k = _kernel(); handle = k.OpenProcess(0x1000 | 0x1 | 0x100000, False, int(expected["pid"]))
    if not handle:
        return False
    try:
        actual = _inspect(k, handle, expected["pid"])
        if not matches(actual, expected, allowed_images):
            return False
        if not k.TerminateProcess(handle, 0):
            error = ctypes.get_last_error()
            # The venv redirector exits by itself when its serving child exits.
            if k.WaitForSingleObject(handle, 1000) == 0:
                return False
            raise ctypes.WinError(error)
        if k.WaitForSingleObject(handle, 10000) != 0:
            raise RuntimeError("服务进程未在停止期限内退出")
        return True
    finally:
        k.CloseHandle(handle)


def launch_processes(token, allowed_images):
    """Recover only this token's Python processes, even before a startup receipt.

    The venv wrapper and actual interpreter are separate processes. A failed
    second spawn must not leave the first interpreter behind during imports.
    """
    if not token or not isinstance(token, str) or not token.isalnum(): return []
    class Entry(ctypes.Structure):
        _fields_ = [("dwSize", w.DWORD), ("cntUsage", w.DWORD), ("th32ProcessID", w.DWORD),
                    ("th32DefaultHeapID", ctypes.c_size_t), ("th32ModuleID", w.DWORD),
                    ("cntThreads", w.DWORD), ("th32ParentProcessID", w.DWORD),
                    ("pcPriClassBase", w.LONG), ("dwFlags", w.DWORD), ("szExeFile", w.WCHAR * 260)]
    k = _kernel()
    k.CreateToolhelp32Snapshot.argtypes = [w.DWORD, w.DWORD]
    k.CreateToolhelp32Snapshot.restype = w.HANDLE
    for name in ("Process32FirstW", "Process32NextW"):
        getattr(k, name).argtypes = [w.HANDLE, ctypes.POINTER(Entry)]
        getattr(k, name).restype = w.BOOL
    snapshot = k.CreateToolhelp32Snapshot(2, 0)
    if snapshot == ctypes.c_void_p(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    records = []
    try:
        entry = Entry(); entry.dwSize = ctypes.sizeof(entry)
        present = k.Process32FirstW(snapshot, ctypes.byref(entry))
        while present:
            if entry.szExeFile.lower() in {"python.exe", "pythonw.exe"}:
                record = identity(entry.th32ProcessID)
                for role in ("console", "watch"):
                    candidate = (record or {}) | {"role": role, "launch_id": token}
                    if matches(record, candidate, allowed_images): records.append(candidate)
            present = k.Process32NextW(snapshot, ctypes.byref(entry))
    finally:
        k.CloseHandle(snapshot)
    return records


def publish_process(role, *, ready=False):
    token = os.environ.get("KNOWLEDGE_BRIDGE_LAUNCH_ID")
    if not token:
        return
    record = identity(os.getpid())
    if not record:
        raise RuntimeError("无法记录服务启动身份")
    record.update(role=role, launch_id=token, ready=ready,
                  vault=os.environ["KNOWLEDGE_BRIDGE_VAULT"],
                  bridge_dir=str(Path(__file__).resolve().parents[1]),
                  local_data=os.environ["KNOWLEDGE_BRIDGE_LOCAL_DATA"])
    atomic_json(Path(record["local_data"]) / "runtime" / f"process-{role}-{token}.json", record)
