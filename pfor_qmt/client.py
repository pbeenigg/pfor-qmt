"""Market-only wrapper over the upstream pipe RPC implementation."""
import threading
from .policy import validate


class CfquantError(RuntimeError):
    def __init__(self, message, remote_type=None):
        super(CfquantError, self).__init__(message)
        self.remote_type = remote_type


class CfquantTimeout(TimeoutError):
    pass


from .pipe_client import PipeRpcClient


class MarketClient(PipeRpcClient):
    def request(self, action, params=None, **kwargs):
        validate(action, params)
        return super(MarketClient, self).request(action, params, **kwargs)


_client = None
_lock = threading.RLock()


def get_client():
    global _client
    with _lock:
        if _client is None:
            _client = MarketClient()
        return _client


def configure(**kwargs):
    global _client
    from .config import configure as apply
    with _lock:
        apply(**kwargs)
        if _client:
            _client.close()
        _client = None
