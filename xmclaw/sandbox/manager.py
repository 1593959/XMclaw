"""Sandbox manager — Docker-first, ProcessRunner fallback."""
from __future__ import annotations
import ast
from xmclaw.utils.log import logger
from .docker_runner import DockerRunner
from .process_runner import ProcessRunner

_DANGEROUS_CALLS = {"exec", "eval", "compile", "__import__"}
_DANGEROUS_IMPORTS = {"subprocess", "ctypes", "cffi", "socket"}


class SandboxManager:
    """Unified sandbox: Docker when available, else subprocess with timeout."""

    def __init__(
        self,
        use_docker: bool = True,
        docker_image: str = "python:3.12-slim",
        timeout: int = 30,
        memory_limit: str = "256m",
    ):
        self.timeout = timeout
        self._docker = (
            DockerRunner(image=docker_image, timeout=timeout, memory_limit=memory_limit)
            if use_docker else None
        )
        self._process = ProcessRunner(timeout=timeout)

    # ── Static analysis ──────────────────────────────────────────────────────

    def lint_python(self, code: str) -> list[str]:
        """Return a list of safety warnings for Python code (AST-based)."""
        warnings: list[str] = []
        try:
            tree = ast.parse(code)
        except SyntaxError as e:
            return [f"SyntaxError: {e}"]

        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                func = node.func
                if isinstance(func, ast.Name) and func.id in _DANGEROUS_CALLS:
                    warnings.append(f"Dangerous builtin: {func.id}()")
                elif isinstance(func, ast.Attribute):
                    full = f"{getattr(func.value, 'id', '?')}.{func.attr}"
                    if full in {"os.system", "os.popen", "subprocess.run", "subprocess.call"}:
                        warnings.append(f"Dangerous call: {full}()")
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                mods = (
                    [a.name for a in node.names]
                    if isinstance(node, ast.Import)
                    else ([node.module] if node.module else [])
                )
                for mod in mods:
                    if mod.split(".")[0] in _DANGEROUS_IMPORTS:
                        warnings.append(f"Potentially dangerous import: {mod}")
        return warnings

    def check_complexity(self, code: str) -> dict:
        """Estimate code complexity from AST (no external tools required)."""
        try:
            tree = ast.parse(code)
            branch_types = (ast.If, ast.While, ast.For, ast.ExceptHandler, ast.With, ast.AsyncWith)
            return {
                "nodes": sum(1 for _ in ast.walk(tree)),
                "functions": sum(1 for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))),
                "classes": sum(1 for n in ast.walk(tree) if isinstance(n, ast.ClassDef)),
                "branches": sum(1 for n in ast.walk(tree) if isinstance(n, branch_types)),
            }
        except SyntaxError:
            return {}

    # ── Execution ────────────────────────────────────────────────────────────

    async def run_python(self, code: str, stdin: str = "", use_docker: bool | None = None) -> dict:
        """Execute Python safely. Returns {stdout, stderr, exit_code, backend, warnings}."""
        warnings = self.lint_python(code)
        prefer_docker = use_docker if use_docker is not None else (self._docker is not None)

        if prefer_docker and self._docker and self._docker.available:
            result = await self._docker.run_python(code, stdin)
            backend = "docker"
        else:
            result = await self._process.run_python(code, stdin)
            backend = "process"

        result["backend"] = backend
        result["warnings"] = warnings
        return result

    async def run_code(self, language: str, code: str, stdin: str = "") -> dict:
        """Execute code in any supported language."""
        if language in ("python", "python3", "py"):
            return await self.run_python(code, stdin)
        if self._docker and self._docker.available:
            result = await self._docker.run_code(language, code, stdin)
            result["backend"] = "docker"
            result["warnings"] = []
            return result
        return {
            "stdout": "",
            "stderr": f"Language '{language}' requires Docker (not available).",
            "exit_code": -1,
            "timed_out": False,
            "backend": "none",
            "warnings": [],
        }


# Global singleton
_manager: SandboxManager | None = None


def get_sandbox() -> SandboxManager:
    global _manager
    if _manager is None:
        _manager = SandboxManager()
    return _manager
