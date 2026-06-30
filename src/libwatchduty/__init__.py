"""Watch Duty API client library.

Exports WatchDutyClient (HTTP client) and WatchDutyError (raised on non-2xx responses).
"""

from .client import WatchDutyClient, WatchDutyError
from .threat import ThreatFactors, compute_threat

__all__ = [
    "WatchDutyClient",
    "WatchDutyError",
    "ThreatFactors",
    "compute_threat",
]
__version__ = "0.1.0"
