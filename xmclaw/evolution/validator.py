"""Validation framework: run generated code and verify it works."""
import ast
import asyncio
import importlib.util
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

from xmclaw.utils.log import logger


class EvolutionValidator:
    """Validate generated Gene/Skill code before solidifying."""

    def validate_python_syntax(self, file_path: Path) -> tuple[bool, str]:
        """Check if Python file has valid syntax."""
        try:
            source = file_path.read_text(encoding="utf-8")
            ast.parse(source)
            return True, "Syntax OK"
        except SyntaxError as e:
            return False, f"SyntaxError: {e}"

    def validate_imports(self, file_path: Path) -> tuple[bool, str]:
        """Try to import the module without executing it."""
        try:
            module_name = file_path.stem
            result = subprocess.run(
                [sys.executable, "-c", f"import importlib.util; spec = importlib.util.spec_from_file_location('{module_name}', r'{file_path}'); mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod)"],
                capture_output=True,
                text=True,
                timeout=10,
            )
            if result.returncode == 0:
                return True, "Import OK"
            return False, f"ImportError: {result.stderr}"
        except subprocess.TimeoutExpired:
            return False, "Import timeout"
        except Exception as e:
            return False, f"Validation error: {e}"

    async def validate_gene_runtime(self, gene_path: Path) -> tuple[bool, str]:
        """Actually instantiate and run the Gene's evaluate/execute methods."""
        try:
            spec = importlib.util.spec_from_file_location(gene_path.stem, str(gene_path))
            if not spec or not spec.loader:
                return False, "Failed to create module spec"
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)

            # Find GeneBase subclass
            from xmclaw.genes.base import GeneBase
            gene_cls = None
            for attr_name in dir(module):
                attr = getattr(module, attr_name)
                if isinstance(attr, type) and issubclass(attr, GeneBase) and attr is not GeneBase:
                    gene_cls = attr
                    break

            if not gene_cls:
                return False, "No GeneBase subclass found"

            instance = gene_cls()
            test_context = {"user_input": "test input for gene validation"}
            matched = await asyncio.wait_for(instance.evaluate(test_context), timeout=10)
            if matched:
                result = await asyncio.wait_for(instance.execute(test_context), timeout=10)
                if not isinstance(result, str):
                    return False, f"execute() must return str, got {type(result)}"
            return True, "Runtime OK"
        except asyncio.TimeoutError:
            return False, "Runtime timeout"
        except Exception as e:
            return False, f"Runtime error: {e}"

    async def validate_skill_runtime(self, skill_path: Path) -> tuple[bool, str]:
        """Actually instantiate and run the Skill's execute method."""
        try:
            spec = importlib.util.spec_from_file_location(skill_path.stem, str(skill_path))
            if not spec or not spec.loader:
                return False, "Failed to create module spec"
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)

            # Find Tool subclass
            from xmclaw.tools.base import Tool
            skill_cls = None
            for attr_name in dir(module):
                attr = getattr(module, attr_name)
                if isinstance(attr, type) and issubclass(attr, Tool) and attr is not Tool:
                    skill_cls = attr
                    break

            if not skill_cls:
                return False, "No Tool subclass found"

            instance = skill_cls()
            # Build dummy args from parameters schema
            dummy_args = {}
            for param_name, param_schema in instance.parameters.items():
                ptype = param_schema.get("type", "string")
                if ptype == "string":
                    dummy_args[param_name] = "test"
                elif ptype == "integer":
                    dummy_args[param_name] = 1
                elif ptype == "boolean":
                    dummy_args[param_name] = True
                elif ptype == "array":
                    dummy_args[param_name] = []
                elif ptype == "object":
                    dummy_args[param_name] = {}
                else:
                    dummy_args[param_name] = None

            result = await asyncio.wait_for(instance.execute(**dummy_args), timeout=10)
            if not isinstance(result, str):
                return False, f"execute() must return str, got {type(result)}"
            return True, "Runtime OK"
        except asyncio.TimeoutError:
            return False, "Runtime timeout"
        except Exception as e:
            return False, f"Runtime error: {e}"

    async def validate_gene(self, gene_path: Path) -> dict[str, Any]:
        """Full validation pipeline for a Gene."""
        results = {
            "syntax": self.validate_python_syntax(gene_path),
            "import": self.validate_imports(gene_path),
            "runtime": await self.validate_gene_runtime(gene_path),
        }
        results["passed"] = all(r[0] for r in results.values())
        logger.info("gene_validated", path=str(gene_path), passed=results["passed"])
        return results

    async def validate_skill(self, skill_path: Path) -> dict[str, Any]:
        """Full validation pipeline for a Skill."""
        results = {
            "syntax": self.validate_python_syntax(skill_path),
            "import": self.validate_imports(skill_path),
            "runtime": await self.validate_skill_runtime(skill_path),
        }
        results["passed"] = all(r[0] for r in results.values())
        logger.info("skill_validated", path=str(skill_path), passed=results["passed"])
        return results
