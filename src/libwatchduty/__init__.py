"""Watch Duty API client library.

Exports WatchDutyClient (HTTP client) and WatchDutyError (raised on non-2xx responses).
"""

from .client import WatchDutyClient, WatchDutyError

__all__ = ["WatchDutyClient", "WatchDutyError"]
__version__ = "0.1.0"
