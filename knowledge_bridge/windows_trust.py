"""Current-user Windows trust. No machine-wide keys or HTTP identity claims."""
import ctypes
from ctypes import wintypes as w
import os
import socket


class DeviceTrustUnavailable(OSError):
    pass


def dpapi(raw: bytes, entropy: bytes, *, decrypt=False) -> bytes:
    if os.name != 'nt':
        raise DeviceTrustUnavailable('设备托管需要 Windows 当前账户')
    class Blob(ctypes.Structure):
        _fields_ = [('size', w.DWORD), ('data', ctypes.POINTER(ctypes.c_ubyte))]
    def blob(value):
        buf = ctypes.create_string_buffer(value)
        return buf, Blob(len(value), ctypes.cast(buf, ctypes.POINTER(ctypes.c_ubyte)))
    source_buffer, source = blob(raw)
    entropy_buffer, additional = blob(entropy)
    output = Blob()
    crypt = ctypes.WinDLL('crypt32', use_last_error=True)
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.LocalFree.argtypes = [ctypes.c_void_p]; kernel.LocalFree.restype = ctypes.c_void_p
    fn = crypt.CryptUnprotectData if decrypt else crypt.CryptProtectData
    fn.argtypes = [ctypes.POINTER(Blob), ctypes.c_void_p, ctypes.POINTER(Blob),
                   ctypes.c_void_p, ctypes.c_void_p, w.DWORD, ctypes.POINTER(Blob)]
    fn.restype = w.BOOL
    try:
        # CRYPTPROTECT_UI_FORBIDDEN only: never LOCAL_MACHINE.
        if not fn(ctypes.byref(source), None, ctypes.byref(additional), None, None, 1, ctypes.byref(output)):
            raise DeviceTrustUnavailable('Windows 账户无法读取设备托管材料，请使用旧口令或恢复密钥')
        return ctypes.string_at(output.data, output.size)
    finally:
        if output.data:
            ctypes.memset(output.data, 0, output.size)
            kernel.LocalFree(output.data)
        ctypes.memset(source_buffer, 0, len(source_buffer))


def _token_sid(process, kernel, advapi):
    token = w.HANDLE()
    if not advapi.OpenProcessToken(process, 8, ctypes.byref(token)):
        raise DeviceTrustUnavailable('无法核验 Windows 账户')
    try:
        needed = w.DWORD()
        advapi.GetTokenInformation(token, 1, None, 0, ctypes.byref(needed))
        if not 0 < needed.value < 65536:
            raise DeviceTrustUnavailable('Windows 账户信息无效')
        buffer = ctypes.create_string_buffer(needed.value)
        if not advapi.GetTokenInformation(token, 1, buffer, len(buffer), ctypes.byref(needed)):
            raise DeviceTrustUnavailable('无法核验 Windows 账户')
        sid = ctypes.c_void_p.from_buffer(buffer).value
        length = advapi.GetLengthSid(sid)
        if not 0 < length < 1024:
            raise DeviceTrustUnavailable('Windows 账户标识无效')
        return ctypes.string_at(sid, length)
    finally:
        kernel.CloseHandle(token)


def _peer_pid(peer, server):
    # The production launcher binds IPv4 loopback, so IPv6 and proxies fail closed.
    if peer[0] != '127.0.0.1' or server[0] != '127.0.0.1':
        raise DeviceTrustUnavailable('只接受本机直接连接')
    class Row(ctypes.Structure):
        _fields_ = [(name, w.DWORD) for name in ('state', 'local_addr', 'local_port',
                                              'remote_addr', 'remote_port', 'pid')]
    api = ctypes.WinDLL('iphlpapi', use_last_error=True).GetExtendedTcpTable
    api.argtypes = [ctypes.c_void_p, ctypes.POINTER(w.DWORD), w.BOOL, w.ULONG, w.DWORD, w.ULONG]
    api.restype = w.DWORD
    for _ in range(3):
        size = w.DWORD()
        api(None, ctypes.byref(size), False, socket.AF_INET, 5, 0)
        if not 4 <= size.value <= 32 * 1024 * 1024:
            break
        buffer = ctypes.create_string_buffer(size.value)
        result = api(buffer, ctypes.byref(size), False, socket.AF_INET, 5, 0)
        if result == 122:
            continue
        if result:
            break
        count = w.DWORD.from_buffer(buffer).value
        if 4 + count * ctypes.sizeof(Row) > len(buffer):
            break
        matches = set()
        for index in range(count):
            row = Row.from_buffer(buffer, 4 + index * ctypes.sizeof(Row))
            local = (socket.inet_ntoa(int(row.local_addr).to_bytes(4, 'little')), socket.ntohs(row.local_port & 65535))
            remote = (socket.inet_ntoa(int(row.remote_addr).to_bytes(4, 'little')), socket.ntohs(row.remote_port & 65535))
            if row.state == 5 and local == peer and remote == server:
                matches.add(int(row.pid))
        if len(matches) == 1:
            return matches.pop()
        break
    raise DeviceTrustUnavailable('无法确认本机连接的 Windows 账户')


def verify_current_user_peer(peer, server) -> None:
    """Bind the live TCP peer to the service's OS user, rechecking PID ownership."""
    if os.name != 'nt' or not peer or not server:
        raise DeviceTrustUnavailable('无法使用 Windows 本机身份')
    peer, server = tuple(peer[:2]), tuple(server[:2])
    pid = _peer_pid(peer, server)
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    advapi = ctypes.WinDLL('advapi32', use_last_error=True)
    kernel.OpenProcess.argtypes = [w.DWORD, w.BOOL, w.DWORD]; kernel.OpenProcess.restype = w.HANDLE
    kernel.GetCurrentProcess.restype = w.HANDLE
    kernel.CloseHandle.argtypes = [w.HANDLE]
    advapi.OpenProcessToken.argtypes = [w.HANDLE, w.DWORD, ctypes.POINTER(w.HANDLE)]
    advapi.GetTokenInformation.argtypes = [w.HANDLE, ctypes.c_int, ctypes.c_void_p, w.DWORD, ctypes.POINTER(w.DWORD)]
    advapi.GetLengthSid.argtypes = [ctypes.c_void_p]; advapi.GetLengthSid.restype = w.DWORD
    handle = kernel.OpenProcess(0x1000, False, pid)
    if not handle:
        raise DeviceTrustUnavailable('无法核验本机连接进程')
    try:
        if _token_sid(handle, kernel, advapi) != _token_sid(kernel.GetCurrentProcess(), kernel, advapi):
            raise DeviceTrustUnavailable('连接来自其他 Windows 账户')
        if _peer_pid(peer, server) != pid:
            raise DeviceTrustUnavailable('本机连接已变化')
    finally:
        kernel.CloseHandle(handle)
