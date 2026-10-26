# -*- coding: utf-8 -*-
"""凭据加密

同花顺自动登录需要账号密码。把券商密码明文写在配置文件里是很危险的事，
所以这里用 Windows 自带的 DPAPI 加密：

    * 加密用的是【当前 Windows 用户】的密钥
    * 只有同一台电脑、同一个 Windows 账号才能解开
    * 就算别人把你的 data 目录整个拷走，也解不开

如果 DPAPI 不可用，程序会【拒绝保存密码】，而不是退回明文存储。
"""
from __future__ import annotations

import base64
import ctypes
import ctypes.wintypes as wt
import threading

from . import store

_lock = threading.RLock()
_PREFIX = "dpapi:"


class _BLOB(ctypes.Structure):
    _fields_ = [("cbData", wt.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]


_crypt32 = None
_kernel32 = None
_init_error = ""


def _init():
    """初始化 crypt32：一定要设置 argtypes，否则 64 位下指针会被截断"""
    global _crypt32, _kernel32, _init_error
    if _crypt32 is not None or _init_error:
        return
    try:
        _crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
        _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        _crypt32.CryptProtectData.argtypes = [
            ctypes.POINTER(_BLOB), wt.LPCWSTR, ctypes.POINTER(_BLOB),
            ctypes.c_void_p, ctypes.c_void_p, wt.DWORD, ctypes.POINTER(_BLOB)]
        _crypt32.CryptProtectData.restype = wt.BOOL
        _crypt32.CryptUnprotectData.argtypes = [
            ctypes.POINTER(_BLOB), ctypes.POINTER(wt.LPWSTR), ctypes.POINTER(_BLOB),
            ctypes.c_void_p, ctypes.c_void_p, wt.DWORD, ctypes.POINTER(_BLOB)]
        _crypt32.CryptUnprotectData.restype = wt.BOOL
    except Exception as e:  # noqa: BLE001
        _init_error = str(e)[:120]


def _to_blob(data: bytes) -> _BLOB:
    buf = ctypes.create_string_buffer(data, len(data))
    return _BLOB(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))


def _from_blob(blob: _BLOB) -> bytes:
    return ctypes.string_at(blob.pbData, blob.cbData)


def available() -> bool:
    """DPAPI 能不能用。注意：探测时输入必须非空，空数据会返回参数错误。"""
    _init()
    if _crypt32 is None:
        return False
    try:
        bin_in = _to_blob(b"probe")
        bin_out = _BLOB()
        ok = _crypt32.CryptProtectData(
            ctypes.byref(bin_in), None, None, None, None, 0, ctypes.byref(bin_out))
        if ok and bin_out.pbData:
            _kernel32.LocalFree(bin_out.pbData)
        return bool(ok)
    except Exception:
        return False


def protect(text: str) -> str:
    _init()
    if not text:
        return ""
    if _crypt32 is None:
        raise OSError("系统加密组件不可用")
    bin_in = _to_blob(text.encode("utf-8"))
    bin_out = _BLOB()
    ok = _crypt32.CryptProtectData(
        ctypes.byref(bin_in), None, None, None, None, 0, ctypes.byref(bin_out))
    if not ok:
        raise OSError("系统加密失败（错误码 %s）" % ctypes.get_last_error())
    try:
        return _PREFIX + base64.b64encode(_from_blob(bin_out)).decode("ascii")
    finally:
        try:
            _kernel32.LocalFree(bin_out.pbData)
        except Exception:
            pass


def unprotect(blob: str) -> str:
    _init()
    if not blob:
        return ""
    if not blob.startswith(_PREFIX):
        # 不是我们加密的东西，坚决不当密码用
        return ""
    if _crypt32 is None:
        return ""
    try:
        raw = base64.b64decode(blob[len(_PREFIX):])
    except Exception:
        return ""
    bin_in = _to_blob(raw)
    bin_out = _BLOB()
    ok = _crypt32.CryptUnprotectData(
        ctypes.byref(bin_in), None, None, None, None, 0, ctypes.byref(bin_out))
    if not ok:
        return ""
    try:
        return _from_blob(bin_out).decode("utf-8", "replace")
    finally:
        try:
            _kernel32.LocalFree(bin_out.pbData)
        except Exception:
            pass


# ---------------------------------------------------------------------------
# 存 / 取 / 删
# ---------------------------------------------------------------------------
def save(name: str, value: str) -> dict:
    """保存一个凭据。返回 {ok, message}"""
    with _lock:
        if not value:
            return {"ok": False, "message": "内容为空"}
        if not available():
            return {"ok": False,
                    "message": "这台电脑上的系统加密（DPAPI）不可用，"
                               "为了安全我拒绝把密码明文存下来。请改为手动登录同花顺下单窗口。"}
        try:
            store.kv_set("secret_" + name, protect(value))
            return {"ok": True, "message": "已用系统加密保存（只有你这台电脑能解开）"}
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "message": "保存失败: %s" % str(e)[:150]}


def load(name: str) -> str:
    with _lock:
        try:
            blob = store.kv_get("secret_" + name)
        except Exception:
            return ""
        if not blob:
            return ""
        return unprotect(blob)


def has(name: str) -> bool:
    try:
        return bool(store.kv_get("secret_" + name))
    except Exception:
        return False


def clear(name: str) -> dict:
    with _lock:
        try:
            store.execute("DELETE FROM kv WHERE key=?", ("secret_" + name,))
            return {"ok": True, "message": "已删除保存的凭据"}
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "message": str(e)[:150]}
