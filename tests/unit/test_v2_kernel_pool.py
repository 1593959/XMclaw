"""Tests for the persistent IPython kernel pool.

Wave-27 fix-LAT2 (2026-05-16). Pins the behavior the user explicitly
asked for after the 5-in-a-row "变量丢了" episode:

  1. State persists across calls in the SAME session.
  2. Sessions are isolated: a variable bound in session A is not
     visible in session B.
  3. ``reset_session`` wipes the namespace.
  4. Exceptions inside user code surface as ``stderr`` + ``returncode=1``
     without killing the kernel; the next call still sees prior state.
  5. Timeout interrupts execution but keeps the kernel alive.
  6. ``automation._code_python`` routes through the pool when one is
     wired AND the call carries a session_id; falls back to subprocess
     otherwise.
"""
from __future__ import annotations

import asyncio
import json

import pytest


def _skip_if_no_deps() -> None:
    try:
        import jupyter_client  # noqa: F401
        import ipykernel  # noqa: F401
    except ImportError:
        pytest.skip("jupyter_client / ipykernel not installed")


# ── core KernelPool behavior ────────────────────────────────────────


@pytest.mark.asyncio
async def test_state_persists_across_calls_in_same_session() -> None:
    """The original bug — ``df`` from call 1 must still exist at call 2."""
    _skip_if_no_deps()
    from xmclaw.providers.tool.kernel_pool import KernelPool
    pool = KernelPool()
    try:
        r1 = await pool.execute("sess-A", "x = 42")
        assert r1.returncode == 0, r1.stderr

        r2 = await pool.execute("sess-A", "print(x * 2)")
        assert r2.returncode == 0, r2.stderr
        assert "84" in r2.stdout
    finally:
        await pool.shutdown_all()


@pytest.mark.asyncio
async def test_sessions_are_isolated() -> None:
    """Two parallel sessions can each have their own ``df`` without
    crosstalk."""
    _skip_if_no_deps()
    from xmclaw.providers.tool.kernel_pool import KernelPool
    pool = KernelPool()
    try:
        await pool.execute("sess-A", "x = 'apple'")
        await pool.execute("sess-B", "x = 'banana'")

        ra = await pool.execute("sess-A", "print(x)")
        rb = await pool.execute("sess-B", "print(x)")
        assert "apple" in ra.stdout
        assert "banana" in rb.stdout
    finally:
        await pool.shutdown_all()


@pytest.mark.asyncio
async def test_reset_session_wipes_namespace() -> None:
    _skip_if_no_deps()
    from xmclaw.providers.tool.kernel_pool import KernelPool
    pool = KernelPool()
    try:
        await pool.execute("sess-C", "y = 99")
        await pool.reset_session("sess-C")
        # New kernel, ``y`` should NOT be bound.
        r = await pool.execute("sess-C", "print(y)")
        assert r.returncode == 1
        assert "NameError" in r.stderr
    finally:
        await pool.shutdown_all()


@pytest.mark.asyncio
async def test_user_exception_does_not_kill_kernel() -> None:
    """Python errors come back as stderr + returncode=1, and the kernel
    keeps running with its prior state intact."""
    _skip_if_no_deps()
    from xmclaw.providers.tool.kernel_pool import KernelPool
    pool = KernelPool()
    try:
        await pool.execute("sess-D", "keep_me = 'alive'")
        bad = await pool.execute("sess-D", "raise ValueError('boom')")
        assert bad.returncode == 1
        assert "ValueError" in bad.stderr
        assert "boom" in bad.stderr

        # Kernel survives — keep_me is still here.
        good = await pool.execute("sess-D", "print(keep_me)")
        assert good.returncode == 0
        assert "alive" in good.stdout
    finally:
        await pool.shutdown_all()


@pytest.mark.asyncio
async def test_timeout_interrupts_but_keeps_kernel() -> None:
    """``time.sleep(10)`` with timeout_s=1 surfaces returncode=124 and
    interrupts, but the next call still works against the same kernel."""
    _skip_if_no_deps()
    from xmclaw.providers.tool.kernel_pool import KernelPool
    pool = KernelPool()
    try:
        await pool.execute("sess-E", "marker = 'before'")
        slow = await pool.execute(
            "sess-E", "import time; time.sleep(10)",
            timeout_s=1.0,
        )
        assert slow.returncode == 124
        assert "exceeded" in slow.stderr

        # Kernel survived → marker still bound.
        after = await pool.execute("sess-E", "print(marker)")
        assert after.returncode == 0
        assert "before" in after.stdout
    finally:
        await pool.shutdown_all()


@pytest.mark.asyncio
async def test_last_expression_value_returned_as_result() -> None:
    """Snippets ending in an expression get its repr in ``result_repr``."""
    _skip_if_no_deps()
    from xmclaw.providers.tool.kernel_pool import KernelPool
    pool = KernelPool()
    try:
        r = await pool.execute("sess-F", "2 + 2")
        assert r.returncode == 0
        assert "4" in r.result_repr
    finally:
        await pool.shutdown_all()


# ── automation._code_python routing ─────────────────────────────────


@pytest.mark.asyncio
async def test_automation_routes_to_pool_when_wired_and_sid_present() -> None:
    """When ``default_pool()`` returns a pool AND the call carries a
    session_id, the tool runs through the persistent kernel."""
    _skip_if_no_deps()
    from xmclaw.core.ir import ToolCall
    from xmclaw.providers.tool.automation import AutomationTools
    from xmclaw.providers.tool.kernel_pool import (
        KernelPool, set_default_pool,
    )

    pool = KernelPool()
    set_default_pool(pool)
    try:
        tools = AutomationTools()
        # Call 1: bind ``v``.
        c1 = ToolCall(
            name="code_python",
            args={"code": "v = 7"},
            provenance="synthetic",
            session_id="route-sess",
        )
        r1 = await tools.invoke(c1)
        assert r1.ok, r1.error
        payload1 = json.loads(r1.content)
        assert payload1["returncode"] == 0

        # Call 2: read ``v`` — must succeed if and only if routing
        # to the persistent kernel worked.
        c2 = ToolCall(
            name="code_python",
            args={"code": "print(v + 1)"},
            provenance="synthetic",
            session_id="route-sess",
        )
        r2 = await tools.invoke(c2)
        assert r2.ok, r2.error
        payload2 = json.loads(r2.content)
        assert payload2["returncode"] == 0
        assert "8" in payload2["stdout"]
    finally:
        await pool.shutdown_all()
        set_default_pool(None)


@pytest.mark.asyncio
async def test_automation_falls_back_to_subprocess_without_session_id() -> None:
    """Tool calls without a session_id (e.g. CLI one-shot mode) hit
    the subprocess path even when a pool is wired."""
    _skip_if_no_deps()
    from xmclaw.core.ir import ToolCall
    from xmclaw.providers.tool.automation import AutomationTools
    from xmclaw.providers.tool.kernel_pool import (
        KernelPool, set_default_pool,
    )

    pool = KernelPool()
    set_default_pool(pool)
    try:
        tools = AutomationTools()
        # No session_id → subprocess path. Should still work.
        call = ToolCall(
            name="code_python",
            args={"code": "print('hello')"},
            provenance="synthetic",
            session_id=None,
        )
        r = await tools.invoke(call)
        assert r.ok, r.error
        payload = json.loads(r.content)
        assert payload["returncode"] == 0
        assert "hello" in payload["stdout"]
        # Pool was never touched: no kernel exists.
        assert pool.session_ids() == []
    finally:
        await pool.shutdown_all()
        set_default_pool(None)


@pytest.mark.asyncio
async def test_reset_flag_clears_namespace_before_run() -> None:
    """``reset=True`` inside the tool call wipes the namespace, then
    runs ``code`` against the fresh kernel."""
    _skip_if_no_deps()
    from xmclaw.core.ir import ToolCall
    from xmclaw.providers.tool.automation import AutomationTools
    from xmclaw.providers.tool.kernel_pool import (
        KernelPool, set_default_pool,
    )

    pool = KernelPool()
    set_default_pool(pool)
    try:
        tools = AutomationTools()
        # Bind ``q``, then reset, then check it's gone.
        sid = "reset-sess"
        await tools.invoke(ToolCall(
            name="code_python", args={"code": "q = 'kept'"},
            provenance="synthetic", session_id=sid,
        ))
        r = await tools.invoke(ToolCall(
            name="code_python",
            args={"code": "print(q)", "reset": True},
            provenance="synthetic", session_id=sid,
        ))
        payload = json.loads(r.content)
        assert payload["returncode"] == 1, (
            "namespace should be wiped"
        )
        assert "NameError" in payload["stderr"]
    finally:
        await pool.shutdown_all()
        set_default_pool(None)
