"""Market data only. Embedded QMT imports must remain standard-library-only."""

__version__ = "0.1.0"


def __getattr__(name):
    if name == "DataClient":
        from .sdk import DataClient
        return DataClient
    raise AttributeError(name)
