"""Test generation and execution tool."""
import json
import subprocess
from pathlib import Path
from typing import Any

from xmclaw.tools.base import Tool
from xmclaw.llm.router import LLMRouter
from xmclaw.utils.log import logger
from xmclaw.utils.paths import BASE_DIR


class TestTool(Tool):
    name = "test"
    description = (
        "Generate and run tests. Actions: generate (auto-create pytest for a file), "
        "run (run a test file or directory), run_all (run all tests in tests/)."
    )

    def __init__(self, llm_router: LLMRouter | None = None):
        self.llm = llm_router

    def get_schema(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["generate", "run", "run_all"],
                        "description": "Test action",
                    },
                    "target": {
                        "type": "string",
                        "description": "File path for generate, or test path for run",
                    },
                },
                "required": ["action"],
            },
        }

    async def execute(self, action: str, target: str = "", **kwargs) -> str:
        action = action.lower()
        if action == "generate":
            if not target:
                return "[Error: generate requires a target file path]"
            return await self._generate_tests(target)
        elif action == "run":
            if not target:
                return "[Error: run requires a target test path]"
            return self._run_tests(target)
        elif action == "run_all":
            return self._run_tests("")
        else:
            return f"[Error: Unknown action '{action}']"

    async def _generate_tests(self, target: str) -> str:
        path = Path(target)
        if not path.exists():
            # Try relative to BASE_DIR
            path = BASE_DIR / target
            if not path.exists():
                return f"[Error: File not found: {target}]"

        try:
            source = path.read_text(encoding="utf-8")
        except Exception as e:
            return f"[Error reading file: {e}]"

        if not self.llm:
            return "[Error: LLM router not available for test generation]"

        prompt = (
            f"请为以下 Python 文件生成 pytest 单元测试。测试文件应命名为 test_{path.stem}.py，"
            f"放在 tests/ 目录下。只输出测试代码，不要任何解释。\n\n"
            f"文件名: {path.name}\n\n"
            f"```python\n{source}\n```"
        )
        messages = [
            {"role": "system", "content": "你是一个专业的 Python 测试工程师，擅长用 pytest 编写单元测试。"},
            {"role": "user", "content": prompt},
        ]

        try:
            response = ""
            async for chunk in self.llm.stream(messages):
                response += chunk

            # Extract code block
            import re
            match = re.search(r"```python\s*([\s\S]*?)```", response)
            if match:
                test_code = match.group(1).strip()
            else:
                test_code = response.strip()

            tests_dir = BASE_DIR / "tests"
            tests_dir.mkdir(exist_ok=True)

            test_file = tests_dir / f"test_{path.stem}.py"
            test_file.write_text(test_code, encoding="utf-8")
            logger.info("tests_generated", target=str(path), test_file=str(test_file))
            return f"Generated tests: {test_file}\n\n{test_code[:500]}"
        except Exception as e:
            logger.error("test_generation_failed", target=str(path), error=str(e))
            return f"[Error generating tests: {e}]"

    def _run_tests(self, target: str) -> str:
        cmd = ["python", "-m", "pytest", "-v", "--tb=short"]
        if target:
            cmd.append(target)
        else:
            cmd.append("tests/")

        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=120,
                cwd=str(BASE_DIR),
            )
            output = result.stdout + "\n" + result.stderr
            logger.info("tests_executed", target=target or "all", returncode=result.returncode)
            return output.strip() or "Tests completed with no output."
        except subprocess.TimeoutExpired:
            return "[Error: Test execution timed out after 120s]"
        except Exception as e:
            logger.error("test_execution_failed", target=target or "all", error=str(e))
            return f"[Error running tests: {e}]"
