"""Gene forge: generate executable Python code from a Gene concept."""
import json
import uuid
from pathlib import Path
from typing import Any

from xmclaw.llm.router import LLMRouter
from xmclaw.utils.log import logger
from xmclaw.utils.paths import BASE_DIR


GENE_TEMPLATE = '''"""
{description}
Auto-generated Gene for XMclaw.
"""
from xmclaw.genes.base import GeneBase

class {class_name}(GeneBase):
    gene_id = "{gene_id}"
    name = "{name}"
    description = """{description}"""
    trigger = "{trigger}"

    async def evaluate(self, context: dict) -> bool:
        """Return True if this gene should activate."""
        user_input = context.get("user_input", "")
        return self.trigger.lower() in user_input.lower()

    async def execute(self, context: dict) -> str:
        """Execute the gene's action."""
        {action_body}
        return "Gene {name} activated."
'''


class GeneForge:
    def __init__(self):
        self.llm = LLMRouter()
        self.output_dir = BASE_DIR / "shared" / "genes"
        self.output_dir.mkdir(parents=True, exist_ok=True)

    async def forge(self, concept: dict[str, Any]) -> dict[str, Any] | None:
        """Turn a gene concept into executable Python code."""
        gene_id = f"gene_{uuid.uuid4().hex[:8]}"
        class_name = self._to_class_name(concept.get("name", "AutoGene"))

        prompt = self._build_prompt(concept, class_name)
        try:
            action_body = await self.llm.complete([{"role": "user", "content": prompt}])
            action_body = action_body.strip()
            if action_body.startswith("```"):
                action_body = action_body.strip("`").replace("python", "").strip()
        except Exception as e:
            logger.error("gene_forge_llm_failed", error=str(e))
            action_body = "pass"

        code = GENE_TEMPLATE.format(
            gene_id=gene_id,
            class_name=class_name,
            name=concept.get("name", "AutoGene"),
            description=concept.get("description", ""),
            trigger=concept.get("trigger", ""),
            action_body=action_body,
        )

        file_path = self.output_dir / f"{gene_id}.py"
        file_path.write_text(code, encoding="utf-8")

        gene = {
            "id": gene_id,
            "name": concept.get("name", "AutoGene"),
            "description": concept.get("description", ""),
            "trigger": concept.get("trigger", ""),
            "action": concept.get("action", ""),
            "path": str(file_path),
            "class_name": class_name,
        }
        logger.info("gene_forged", gene_id=gene_id, path=str(file_path))
        return gene

    def _to_class_name(self, name: str) -> str:
        """Convert a gene name to a valid Python class name."""
        parts = name.replace("-", " ").replace("_", " ").split()
        return "".join(p.capitalize() for p in parts if p)

    def _build_prompt(self, concept: dict[str, Any], class_name: str) -> str:
        return f"""You are generating the body of a Python method for an AI behavior gene.

Gene Name: {concept.get('name')}
Description: {concept.get('description')}
Trigger: {concept.get('trigger')}
Action: {concept.get('action')}

Write ONLY the body of the `execute` method (no method signature, no class wrapper).
The method has access to `context` (dict with "user_input", "agent_id", etc.).
Use `await` for any async operations.

Example output:
    user_input = context.get("user_input", "")
    if "urgent" in user_input:
        return "This seems urgent. I'll prioritize it."
    return "Noted."
"""
