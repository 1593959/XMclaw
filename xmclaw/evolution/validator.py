"""Validation framework: run generated code and verify it works."""
import ast
import subprocess
import sys
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
            # Use python -c to import the module
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

    def validate_gene(self, gene_path: Path) -> dict[str, Any]:
        """Full validation pipeline for a Gene."""
        results = {
            "syntax": self.validate_python_syntax(gene_path),
            "import": self.validate_imports(gene_path),
        }
        results["passed"] = all(r[0] for r in results.values())
        logger.info("gene_validated", path=str(gene_path), passed=results["passed"])
        return results

    def validate_skill(self, skill_path: Path) -> dict[str, Any]:
        """Full validation pipeline for a Skill."""
        results = {
            "syntax": self.validate_python_syntax(skill_path),
            "import": self.validate_imports(skill_path),
        }
        results["passed"] = all(r[0] for r in results.values())
        logger.info("skill_validated", path=str(skill_path), passed=results["passed"])
        return results
