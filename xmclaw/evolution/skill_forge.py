"""Skill forge: generate executable Python code for a Skill."""
import json
import textwrap
import uuid
from pathlib import Path
from typing import Any

from xmclaw.llm.router import LLMRouter
from xmclaw.utils.log import logger
from xmclaw.utils.paths import BASE_DIR


SKILL_TEMPLATE = '''"""
{description}
Auto-generated Skill for XMclaw.
"""
from xmclaw.tools.base import Tool

class {class_name}(Tool):
    name = "{skill_id}"
    description = """{description}"""
    parameters = {parameters}

    async def execute(self, **kwargs) -> str:
        """Execute the skill."""
{action_body}
'''


class SkillForge:
    def __init__(self):
        self.llm = LLMRouter()
        self.output_dir = BASE_DIR / "shared" / "skills"
        self.output_dir.mkdir(parents=True, exist_ok=True)

    async def forge(self, concept: dict[str, Any], action_body: str | None = None) -> dict[str, Any] | None:
        """Turn a skill concept into executable Python code."""
        skill_id = f"skill_{uuid.uuid4().hex[:8]}"
        class_name = self._to_class_name(concept.get("name", "AutoSkill"))

        if action_body is None:
            prompt = self._build_prompt(concept, class_name)
            try:
                code_body = await self.llm.complete([{"role": "user", "content": prompt}])
                code_body = code_body.strip()
                if code_body.startswith("```"):
                    code_body = code_body.strip("`").replace("python", "").strip()
            except Exception as e:
                logger.error("skill_forge_llm_failed", error=str(e))
                code_body = "        return 'Skill executed.'"
        else:
            code_body = action_body

        # Ensure proper indentation for method body (dedent then indent)
        lines = [line for line in code_body.splitlines() if line.strip()]
        if not lines:
            code_body = "        return 'Skill executed.'"
        else:
            dedented = textwrap.dedent(code_body)
            code_body = "\n".join("        " + line for line in dedented.splitlines())

        # Parse parameters from concept or default
        parameters = concept.get("parameters", {
            "input": {"type": "string", "description": "Input for the skill"}
        })

        code = SKILL_TEMPLATE.format(
            skill_id=skill_id,
            class_name=class_name,
            description=concept.get("description", ""),
            parameters=json.dumps(parameters, indent=4),
            action_body=code_body,
        )

        file_path = self.output_dir / f"{skill_id}.py"
        file_path.write_text(code, encoding="utf-8")

        # Also write JSON metadata
        meta = {
            "id": skill_id,
            "name": concept.get("name", "AutoSkill"),
            "category": "auto",
            "version": "v1",
            "description": concept.get("description", ""),
            "path": str(file_path),
        }
        meta_path = self.output_dir / f"{skill_id}.json"
        meta_path.write_text(json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8")

        logger.info("skill_forged", skill_id=skill_id, path=str(file_path))
        return meta

    def _to_class_name(self, name: str) -> str:
        parts = name.replace("-", " ").replace("_", " ").split()
        return "".join(p.capitalize() for p in parts if p)

    def _build_prompt(self, concept: dict[str, Any], class_name: str) -> str:
        return f"""You are generating the body of a Python Tool class method for an AI skill.

Skill Name: {concept.get('name')}
Description: {concept.get('description')}

Write ONLY the body of the `execute` method (no method signature, no class wrapper, no `def` line).
The body will be placed inside:

    async def execute(self, **kwargs) -> str:
        # YOUR CODE HERE

Rules:
1. The method receives keyword arguments via `**kwargs`.
2. Use `await` for any async operations.
3. Return a string result.
4. Do NOT write `return` at the top level. All code must be indented as if inside the method.
5. Do NOT include any markdown code blocks (no ```python).

Example output:
    query = kwargs.get("query", "")
    return f"Searching for: {{query}}"
"""
