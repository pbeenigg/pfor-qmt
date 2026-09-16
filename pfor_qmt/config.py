"""Python 3.6 safe pipe settings, populated from TOML by the host/deployer."""
import os

_config = {
    "pipe_name": os.environ.get("PFOR_QMT_PIPE_NAME", r"\\.\pipe\pfor_qmt_pipe_hub"),
    "request_channel": "pfor_qmt.market.request",
    "timeout": 15.0,
    "pipe_connect_timeout_ms": 1500,
    "heartbeat_seconds": 10.0,
}


def get_config():
    return dict(_config)


def configure(**kwargs):
    if set(kwargs) - set(_config):
        raise ValueError("Unsupported connection setting")
    _config.update(kwargs)
