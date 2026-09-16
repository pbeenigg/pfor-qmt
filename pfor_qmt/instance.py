"""Keep the single local QMT source owned by only one service process."""
import ctypes
import os
from ctypes import wintypes


class ServiceInstance:
    def __init__(self):
        self.handle = None
        if os.name == 'nt':
            self.kernel = ctypes.WinDLL('kernel32',use_last_error=True)
            self.kernel.CreateMutexW.argtypes = [ctypes.c_void_p,wintypes.BOOL,wintypes.LPCWSTR]
            self.kernel.CreateMutexW.restype = wintypes.HANDLE
            self.kernel.CloseHandle.argtypes = [wintypes.HANDLE]
            self.handle = self.kernel.CreateMutexW(None,False,'Local\\pfor_qmt_market_service')
            if not self.handle:
                raise OSError('无法创建 pfor-qmt 服务互斥锁')
            if ctypes.get_last_error() == 183:
                self.close()
                raise RuntimeError('已有 pfor-qmt 服务正在管理该行情源')

    def close(self):
        if self.handle:
            self.kernel.CloseHandle(self.handle)
            self.handle = None
