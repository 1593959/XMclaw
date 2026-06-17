"""LanceDB 操作超时与重试工具。"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Awaitable, TypeVar

_T = TypeVar("_T")

logger = logging.getLogger(__name__)


def _is_transient_lance_error(exc: BaseException) -> bool:
    """判断异常是否为可重试的瞬态 LanceDB 错误。

    覆盖：
    - asyncio.TimeoutError（Python 侧 wait_for 超时）
    - ConnectionError（网络 / 文件句柄问题）
    - RuntimeError("lance error ...") 中带有已知瞬态签名的子集
    """
    if isinstance(exc, asyncio.TimeoutError):
        return True
    if isinstance(exc, ConnectionError):
        return True
    if isinstance(exc, RuntimeError):
        msg = str(exc).lower()
        if "lance error" not in msg:
            return False
        # 明确永久性损坏的签名
        permanent = [
            "corrupt",
            "invalid format",
            "not a lance file",
            "schema mismatch",
            "column not found",
            "field not found",
        ]
        if any(p in msg for p in permanent):
            return False
        # 瞬态签名
        transient = [
            "resource temporarily unavailable",
            "file is locked",
            "no space left",
            "permission denied",
            "device or resource busy",
            "timeout",
            "temporarily unavailable",
            "try again",
            "resource busy",
            "lock",
        ]
        return any(t in msg for t in transient)
    return False


async def _with_timeout_and_retry(
    coro: Awaitable[_T],
    *,
    timeout_s: float = 10.0,
    max_retries: int = 2,
    base_delay: float = 1.0,
    context: str = "",
    corrupted_flag: Any | None = None,
    transient_failures_attr: str = "_transient_failures",
    max_transient_attr: str = "_MAX_TRANSIENT_RETRIES",
) -> _T:
    """用 asyncio.wait_for 给 LanceDB 操作加内层超时，并在瞬态错误时指数退避重试。

    LanceDB 的 async Python API 底层是 Rust I/O，可能不会 yield 回 asyncio loop。
    外层 timeout 对这样的 coroutine 无效；必须用 asyncio.wait_for 在 Python 侧强制截断。

    Args:
        coro: 要保护的 LanceDB 协程（如 table.merge_insert(...).execute()）。
        timeout_s: 单次尝试的最大秒数。默认 10s。
        max_retries: 超时或瞬态错误后的额外尝试次数。默认 2。
        base_delay: 退避基数。重试间隔 = base_delay * (2 ** attempt)。
        context: 日志上下文标识，如 "upsert" / "search" / "neighbors"。
        corrupted_flag: 拥有 ``_corrupted`` bool 属性的对象实例（如 backend self）。
        transient_failures_attr: 对象上记录瞬态失败次数的属性名。
        max_transient_attr: 对象上允许的最大瞬态重试次数的属性名。
    """
    last_exc: BaseException | None = None

    for attempt in range(max_retries + 1):
        try:
            # shield 防止超时 cancellation 把底层 Rust future 留在不确定状态
            return await asyncio.wait_for(
                asyncio.shield(coro),
                timeout=timeout_s,
            )
        except Exception as exc:  # noqa: BLE001
            if not _is_transient_lance_error(exc):
                raise

            last_exc = exc

            # 更新瞬态失败计数器
            if corrupted_flag is not None:
                count = getattr(corrupted_flag, transient_failures_attr, 0) + 1
                setattr(corrupted_flag, transient_failures_attr, count)
                max_allowed = getattr(corrupted_flag, max_transient_attr, 3)
                if count >= max_allowed:
                    if hasattr(corrupted_flag, "_corrupted"):
                        corrupted_flag._corrupted = True
                    logger.error(
                        "lancedb.%s transient failures exhausted (%d/%d) — "
                        "marking backend corrupted",
                        context,
                        count,
                        max_allowed,
                    )
                    raise last_exc from None

            # 指数退避
            delay = base_delay * (2 ** attempt)
            logger.warning(
                "lancedb.%s attempt %d/%d failed (%s: %s), retrying in %.1fs",
                context,
                attempt + 1,
                max_retries + 1,
                type(last_exc).__name__,
                last_exc,
                delay,
            )
            await asyncio.sleep(delay)

    # 理论上不会到达这里（最后一次失败会在循环内 raise），保留防御性代码
    if last_exc is not None:
        if corrupted_flag is not None and hasattr(corrupted_flag, "_corrupted"):
            corrupted_flag._corrupted = True
        logger.error(
            "lancedb.%s failed after %d attempts: %s",
            context,
            max_retries + 1,
            last_exc,
        )
        raise last_exc from None

    raise RuntimeError("Unexpected end of _with_timeout_and_retry")
