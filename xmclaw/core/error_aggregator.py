"""Error aggregation service — collects swallowed errors and reports them."""

from __future__ import annotations

import threading
import time
from collections import defaultdict
from contextlib import contextmanager
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional


class ErrorSeverity(Enum):
    CRITICAL = "critical"    # 影响核心功能，必须立即报告
    WARNING = "warning"      # 影响辅助功能，应记录并定期报告
    INFO = "info"            # 轻微问题，静默处理


@dataclass
class AggregatedError:
    severity: ErrorSeverity
    source: str              # 模块名
    function: str            # 函数名
    error_type: str          # 异常类型
    message: str
    count: int = 1
    last_seen: float = field(default_factory=time.time)
    first_seen: float = field(default_factory=time.time)


class ErrorAggregator:
    """Thread-safe error aggregator."""

    def __init__(self):
        self._errors: dict[str, AggregatedError] = {}
        self._lock = threading.Lock()

    def record(
        self,
        severity: ErrorSeverity,
        source: str,
        function: str,
        error: Exception,
        message: Optional[str] = None,
    ) -> None:
        """Record an error occurrence."""
        key = f"{source}:{function}:{type(error).__name__}"
        with self._lock:
            if key in self._errors:
                self._errors[key].count += 1
                self._errors[key].last_seen = time.time()
            else:
                self._errors[key] = AggregatedError(
                    severity=severity,
                    source=source,
                    function=function,
                    error_type=type(error).__name__,
                    message=message or str(error),
                )

    def get_report(
        self,
        min_severity: ErrorSeverity = ErrorSeverity.WARNING,
    ) -> list[AggregatedError]:
        """Get aggregated errors above minimum severity."""
        severity_order = {
            ErrorSeverity.INFO: 0,
            ErrorSeverity.WARNING: 1,
            ErrorSeverity.CRITICAL: 2,
        }
        with self._lock:
            return [
                e for e in self._errors.values()
                if severity_order[e.severity] >= severity_order[min_severity]
            ]

    def clear(self) -> None:
        """Clear all recorded errors."""
        with self._lock:
            self._errors.clear()


# Global singleton
_global_aggregator: Optional[ErrorAggregator] = None


def get_aggregator() -> ErrorAggregator:
    global _global_aggregator
    if _global_aggregator is None:
        _global_aggregator = ErrorAggregator()
    return _global_aggregator


@contextmanager
def safe_call(
    severity: ErrorSeverity,
    source: str,
    function: str,
    *,
    message: Optional[str] = None,
    reraise: bool = False,
):
    """Context manager for safe error handling with aggregation.

    Usage::

        with safe_call(ErrorSeverity.WARNING, __name__, "my_func"):
            risky_operation()

    Swallows exceptions by default (reraise=False).  When reraise=True
    the exception is re-raised after recording.
    """
    try:
        yield
    except Exception as exc:
        get_aggregator().record(
            severity=severity,
            source=source,
            function=function,
            error=exc,
            message=message,
        )
        if reraise:
            raise
