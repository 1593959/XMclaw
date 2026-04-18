"""Error recovery system for XMclaw with retry logic and graceful degradation.

Provides:
- Exponential backoff retry for transient failures
- Circuit breaker pattern for persistent failures
- Fallback strategies for degraded operation
- Error classification and recovery planning
"""
import asyncio
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Awaitable, Callable, TypeVar
from xmclaw.utils.log import logger

T = TypeVar("T")


class ErrorCategory(Enum):
    """Categories of errors with different recovery strategies."""
    TRANSIENT = "transient"       # Temporary - safe to retry (network timeout, rate limit)
    PERMANENT = "permanent"       # Won't change with retries (auth failure, invalid input)
    RESOURCE = "resource"         # Resource related - retry after backoff (memory, disk)
    UNKNOWN = "unknown"           # Unclassified


@dataclass
class ErrorContext:
    """Context information about an error for recovery planning."""
    error: Exception
    category: ErrorCategory
    operation: str           # What operation was being attempted
    attempt: int = 0        # Current attempt number
    max_attempts: int = 3   # Maximum retry attempts
    timestamp: float = field(default_factory=time.time)
    metadata: dict = field(default_factory=dict)

    @property
    def can_retry(self) -> bool:
        if self.category == ErrorCategory.PERMANENT:
            return False
        if self.category == ErrorCategory.UNKNOWN:
            return self.attempt < self.max_attempts
        return self.attempt < self.max_attempts

    @property
    def backoff_seconds(self) -> float:
        """Exponential backoff with jitter."""
        base = min(2 ** self.attempt, 60)  # Cap at 60 seconds
        import random
        jitter = random.uniform(0, base * 0.1)
        return base + jitter


@dataclass
class RetryPolicy:
    """Configurable retry policy for operations."""
    max_attempts: int = 3
    initial_backoff: float = 1.0
    max_backoff: float = 60.0
    retryable_categories: tuple = (
        ErrorCategory.TRANSIENT,
        ErrorCategory.RESOURCE,
        ErrorCategory.UNKNOWN,
    )


class CircuitBreaker:
    """Circuit breaker pattern to prevent cascading failures.

    States:
    - CLOSED: Normal operation, requests pass through
    - OPEN: Failures exceeded threshold, requests fail fast
    - HALF_OPEN: Testing if service recovered
    """

    class State(Enum):
        CLOSED = "closed"
        OPEN = "open"
        HALF_OPEN = "half_open"

    def __init__(
        self,
        failure_threshold: int = 5,
        recovery_timeout: float = 30.0,
        half_open_max_calls: int = 3,
    ):
        self.failure_threshold = failure_threshold
        self.recovery_timeout = recovery_timeout
        self.half_open_max_calls = half_open_max_calls

        self._state = self.State.CLOSED
        self._failure_count = 0
        self._last_failure_time: float | None = None
        self._half_open_calls = 0
        self._lock = asyncio.Lock()

    @property
    def state(self) -> State:
        return self._state

    async def can_execute(self) -> bool:
        """Check if a request can be executed."""
        async with self._lock:
            if self._state == self.State.CLOSED:
                return True

            if self._state == self.State.OPEN:
                # Check if recovery timeout has passed
                if self._last_failure_time and \
                   time.time() - self._last_failure_time >= self.recovery_timeout:
                    self._state = self.State.HALF_OPEN
                    self._half_open_calls = 0
                    logger.info("circuit_breaker_half_open")
                    return True
                return False

            if self._state == self.State.HALF_OPEN:
                return self._half_open_calls < self.half_open_max_calls

            return False

    async def record_success(self) -> None:
        """Record a successful execution."""
        async with self._lock:
            if self._state == self.State.HALF_OPEN:
                self._half_open_calls += 1
                if self._half_open_calls >= self.half_open_max_calls:
                    self._state = self.State.CLOSED
                    self._failure_count = 0
                    logger.info("circuit_breaker_closed_recovered")

            elif self._state == self.State.CLOSED:
                # Reset failure count on success
                self._failure_count = max(0, self._failure_count - 1)

    async def record_failure(self) -> None:
        """Record a failed execution."""
        async with self._lock:
            self._failure_count += 1
            self._last_failure_time = time.time()

            if self._state == self.State.HALF_OPEN:
                # Any failure in half_open goes back to open
                self._state = self.State.OPEN
                logger.warning("circuit_breaker_reopened", failures=self._failure_count)

            elif self._state == self.State.CLOSED:
                if self._failure_count >= self.failure_threshold:
                    self._state = self.State.OPEN
                    logger.warning("circuit_breaker_opened",
                                 failures=self._failure_count,
                                 threshold=self.failure_threshold)


class ErrorClassifier:
    """Classifies errors into categories with recovery strategies."""

    # Error patterns by category
    TRANSIENT_PATTERNS = [
        "timeout",
        "timed out",
        "rate limit",
        "rate_limit",
        "too many requests",
        "connection reset",
        "connection refused",
        "temporarily unavailable",
        "service unavailable",
        "503",
        "502",
        "429",
        "network error",
        "连接超时",
        "请求过多",
        "暂时不可用",
        "网络错误",
    ]

    PERMANENT_PATTERNS = [
        "authentication",
        "auth",
        "unauthorized",
        "invalid api key",
        "permission denied",
        "forbidden",
        "401",
        "403",
        "invalid request",
        "bad request",
        "400",
        "not found",
        "404",
        "invalid input",
        "认证失败",
        "权限不足",
        "未授权",
    ]

    RESOURCE_PATTERNS = [
        "out of memory",
        "memory error",
        "disk full",
        "disk space",
        "quota exceeded",
        "limit exceeded",
        "内存不足",
        "磁盘空间",
    ]

    @classmethod
    def classify(cls, error: Exception, operation: str = "") -> ErrorCategory:
        """Classify an error based on its message and type."""
        error_str = str(error).lower()

        # Check permanent patterns first (highest priority)
        for pattern in cls.PERMANENT_PATTERNS:
            if pattern in error_str:
                return ErrorCategory.PERMANENT

        # Check transient patterns
        for pattern in cls.TRANSIENT_PATTERNS:
            if pattern in error_str:
                return ErrorCategory.TRANSIENT

        # Check resource patterns
        for pattern in cls.RESOURCE_PATTERNS:
            if pattern in error_str:
                return ErrorCategory.RESOURCE

        # Check by exception type
        error_type = type(error).__name__.lower()

        # Transient exception types
        transient_types = {"timeouterror", "httperror", "clienterror", "asynciotimeout"}
        if error_type in transient_types:
            return ErrorCategory.TRANSIENT

        # Permanent exception types
        permanent_types = {"authenticationerror", "authorisationerror", "valueerror", "typeerror"}
        if error_type in permanent_types:
            return ErrorCategory.PERMANENT

        # Check operation context
        if operation:
            op_lower = operation.lower()
            if any(kw in op_lower for kw in ["auth", "login", "token"]):
                return ErrorCategory.PERMANENT
            if any(kw in op_lower for kw in ["memory", "disk", "storage"]):
                return ErrorCategory.RESOURCE

        return ErrorCategory.UNKNOWN


class ErrorRecovery:
    """Main error recovery orchestrator with retry and fallback capabilities."""

    # Circuit breakers for different operation types
    _breakers: dict[str, CircuitBreaker] = {}

    def __init__(self):
        self.classifier = ErrorClassifier()
        self._init_circuit_breakers()

    def _init_circuit_breakers(self) -> None:
        """Initialize circuit breakers for different operation types."""
        self._breakers = {
            "llm": CircuitBreaker(failure_threshold=5, recovery_timeout=60.0),
            "tool": CircuitBreaker(failure_threshold=3, recovery_timeout=30.0),
            "memory": CircuitBreaker(failure_threshold=3, recovery_timeout=30.0),
            "network": CircuitBreaker(failure_threshold=5, recovery_timeout=45.0),
        }

    def get_breaker(self, operation_type: str) -> CircuitBreaker:
        """Get or create a circuit breaker for an operation type."""
        if operation_type not in self._breakers:
            self._breakers[operation_type] = CircuitBreaker()
        return self._breakers[operation_type]

    async def with_retry(
        self,
        operation: Callable[..., Awaitable[T]],
        operation_name: str,
        *args,
        policy: RetryPolicy | None = None,
        operation_type: str = "general",
        fallback: Callable[..., Awaitable[T]] | None = None,
        **kwargs,
    ) -> T | None:
        """Execute an operation with retry logic and circuit breaker.

        Args:
            operation: The async function to execute
            operation_name: Name for logging
            *args: Positional args for operation
            policy: Retry policy (uses default if None)
            operation_type: Type for circuit breaker (llm, tool, memory, network)
            fallback: Fallback function if all retries fail
            **kwargs: Keyword args for operation

        Returns:
            Result of operation, or fallback result, or None
        """
        policy = policy or RetryPolicy()
        breaker = self.get_breaker(operation_type)

        last_error: Exception | None = None

        for attempt in range(policy.max_attempts):
            # Check circuit breaker
            if not await breaker.can_execute():
                logger.warning("circuit_breaker_rejecting",
                             operation=operation_name,
                             state=breaker.state.value)
                if fallback:
                    return await self._execute_fallback(fallback, operation_name, args, kwargs)
                return None

            try:
                result = await operation(*args, **kwargs)
                await breaker.record_success()
                logger.debug("operation_succeeded", operation=operation_name, attempt=attempt + 1)
                return result

            except Exception as e:
                last_error = e
                ctx = ErrorContext(
                    error=e,
                    category=self.classifier.classify(e, operation_name),
                    operation=operation_name,
                    attempt=attempt + 1,
                    max_attempts=policy.max_attempts,
                )

                await breaker.record_failure()

                if not ctx.can_retry:
                    logger.warning("error_not_retryable",
                                 operation=operation_name,
                                 category=ctx.category.value,
                                 error=str(e)[:100])
                    break

                # Log retry attempt
                logger.info("operation_retry",
                          operation=operation_name,
                          attempt=attempt + 1,
                          max_attempts=policy.max_attempts,
                          backoff=ctx.backoff_seconds,
                          category=ctx.category.value,
                          error=str(e)[:80])

                # Wait before retry
                if attempt < policy.max_attempts - 1:
                    await asyncio.sleep(ctx.backoff_seconds)

        # All retries exhausted
        logger.error("operation_failed_all_retries",
                    operation=operation_name,
                    attempts=policy.max_attempts,
                    error=str(last_error)[:100] if last_error else "unknown")

        # Try fallback
        if fallback:
            return await self._execute_fallback(fallback, operation_name, args, kwargs)

        return None

    async def _execute_fallback(
        self,
        fallback: Callable[..., Awaitable[T]],
        operation_name: str,
        args: tuple,
        kwargs: dict,
    ) -> T | None:
        """Execute a fallback function with error handling."""
        try:
            logger.info("executing_fallback", operation=operation_name)
            return await fallback(*args, **kwargs)
        except Exception as e:
            logger.error("fallback_also_failed",
                       operation=operation_name,
                       error=str(e)[:100])
            return None

    def get_recovery_status(self) -> dict[str, Any]:
        """Get status of all circuit breakers for monitoring."""
        return {
            "circuit_breakers": {
                name: {
                    "state": breaker.state.value,
                    "failure_count": breaker._failure_count,
                    "last_failure": breaker._last_failure_time,
                }
                for name, breaker in self._breakers.items()
            }
        }


# Global singleton instance
_recovery: ErrorRecovery | None = None


def get_error_recovery() -> ErrorRecovery:
    """Get the global error recovery instance."""
    global _recovery
    if _recovery is None:
        _recovery = ErrorRecovery()
    return _recovery
