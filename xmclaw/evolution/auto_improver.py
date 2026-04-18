"""Auto-improver: turn reflection results into Genes, Skills, or core patch proposals."""
import json
import subprocess
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

from xmclaw.llm.router import LLMRouter
from xmclaw.evolution.gene_forge import GeneForge
from xmclaw.evolution.skill_forge import SkillForge
from xmclaw.evolution.validator import EvolutionValidator
from xmclaw.utils.log import logger
from xmclaw.utils.paths import BASE_DIR


class AutoImprover:
    """
    Safe self-improvement pipeline:
    - Layer 2 (Genes/Skills): auto-generate, validate, hot-reload
    - Layer 3 (Core framework): generate patch file, wait for human approval
    """

    def __init__(self):
        self.llm = LLMRouter()
        self.gene_forge = GeneForge()
        self.skill_forge = SkillForge()
        self.validator = EvolutionValidator()
        self.proposals_dir = BASE_DIR / "shared" / "proposals"
        self.proposals_dir.mkdir(parents=True, exist_ok=True)

    async def improve_from_reflection(self, agent_id: str, reflection: dict) -> dict[str, Any]:
        """
        Analyze reflection and decide what to improve.
        Returns a report of actions taken or proposed.
        """
        problems = reflection.get("problems", [])
        lessons = reflection.get("lessons", [])
        improvements = reflection.get("improvements", [])

        if not any([problems, lessons, improvements]):
            return {"status": "nothing_to_improve"}

        # Build a decision prompt
        context = {
            "problems": problems,
            "lessons": lessons,
            "improvements": improvements,
        }
        prompt = self._build_decision_prompt(context)

        try:
            text = await self.llm.complete([{"role": "user", "content": prompt}])
            text = text.strip().strip("`").replace("json", "").strip()
            decision = json.loads(text)
        except Exception as e:
            logger.error("auto_improver_decision_failed", error=str(e), raw=text[:500] if "text" in dir() else "")
            return {"status": "decision_failed", "error": str(e)}

        results = {"status": "processing", "actions": []}
        action = decision.get("action", "none")

        if action == "gene":
            result = await self._generate_gene(decision, context)
            results["actions"].append(result)
        elif action == "skill":
            result = await self._generate_skill(decision, context)
            results["actions"].append(result)
        elif action == "patch":
            result = await self._generate_patch(decision, context)
            results["actions"].append(result)
        else:
            results["actions"].append({"type": "none", "reason": decision.get("reason", "No action needed")})

        return results

    def _build_decision_prompt(self, context: dict) -> str:
        return f"""\n你是一位 AI 自我改进策略师。请根据以下反思结果，决定最佳的改进方式。

反思结果:
{json.dumps(context, ensure_ascii=False, indent=2)}

可选的改进方式:
1. gene — 生成一个行为基因 (Gene)，注入到 system prompt 中，修正 Agent 的行为模式
2. skill — 生成一个可执行技能 (Skill)，补充 Agent 的工具能力
3. patch — 需要修改核心框架代码（如 agent_loop.py, server.py 等），只能生成补丁提案，等待人工审批
4. none — 不需要自动改进

请用 JSON 输出你的决策:
{{
  "action": "gene|skill|patch|none",
  "reason": "为什么选这个",
  "target": "如果是 patch，写目标文件路径；如果是 gene/skill，写概念名称",
  "concept": {{
    "name": "名称",
    "description": "一句话描述",
    "trigger": "触发关键词（gene 必填）"
  }},
  "action_body": "如果是 gene/skill，写 execute() 的方法体 Python 代码"
}}
"""

    async def _generate_gene(self, decision: dict, context: dict) -> dict[str, Any]:
        concept = decision.get("concept", {})
        concept.setdefault("name", "auto_reflection_gene")
        concept.setdefault("description", context.get("lessons", ["Auto-generated from reflection"])[0])
        concept.setdefault("trigger", " ".join(context.get("problems", []))[:50])

        action_body = decision.get("action_body", "        pass")

        # Auto-commit before modification
        self._auto_commit(f"auto-improver: prepare gene {concept['name']}")

        try:
            gene = await self.gene_forge.forge(concept, action_body=action_body)
            if not gene:
                return {"type": "gene", "status": "forge_failed"}

            validation = await self.validator.validate_gene(Path(gene["path"]))
            if not validation["passed"]:
                logger.warning("auto_improver_gene_validation_failed", gene_id=gene["id"], errors=validation)
                return {"type": "gene", "status": "validation_failed", "gene_id": gene["id"], "errors": validation}

            # Save to DB
            from xmclaw.memory.sqlite_store import SQLiteStore
            db = SQLiteStore(BASE_DIR / "shared" / "memory.db")
            db.insert_gene("default", gene)

            logger.info("auto_improver_gene_created", gene_id=gene["id"], name=gene["name"])
            return {"type": "gene", "status": "created", "gene_id": gene["id"], "name": gene["name"]}
        except Exception as e:
            logger.error("auto_improver_gene_error", error=str(e))
            return {"type": "gene", "status": "error", "error": str(e)}

    async def _generate_skill(self, decision: dict, context: dict) -> dict[str, Any]:
        concept = decision.get("concept", {})
        concept.setdefault("name", "auto_reflection_skill")
        concept.setdefault("description", context.get("lessons", ["Auto-generated from reflection"])[0])
        concept.setdefault("parameters", {"input": {"type": "string", "description": "Input"}})

        action_body = decision.get("action_body", "        return 'Skill executed.'")

        # Auto-commit before modification
        self._auto_commit(f"auto-improver: prepare skill {concept['name']}")

        try:
            skill = await self.skill_forge.forge(concept, action_body=action_body)
            if not skill:
                return {"type": "skill", "status": "forge_failed"}

            validation = await self.validator.validate_skill(Path(skill["path"]))
            if not validation["passed"]:
                logger.warning("auto_improver_skill_validation_failed", skill_id=skill["id"], errors=validation)
                return {"type": "skill", "status": "validation_failed", "skill_id": skill["id"], "errors": validation}

            # Register in DB
            from xmclaw.memory.sqlite_store import SQLiteStore
            db = SQLiteStore(BASE_DIR / "shared" / "memory.db")
            db.insert_skill("default", skill)

            # Hot-reload: register the new skill into the shared live tool registry
            # so it becomes immediately available for the next LLM tool-calling decision.
            try:
                from xmclaw.tools.registry import ToolRegistry
                from xmclaw.core.event_bus import Event, EventType, get_event_bus
                shared = ToolRegistry.get_shared()
                if shared is not None:
                    # Load just the new skill file into the live registry
                    await shared._load_generated_skills()
                    # Also publish event so frontend can show real-time feedback
                    bus = get_event_bus()
                    await bus.publish(Event(
                        event_type=EventType.SKILL_EXECUTED,
                        source=self.__class__.__name__,
                        payload={
                            "skill_id": skill["id"],
                            "skill_name": skill.get("name"),
                            "action": "hot_reloaded",
                        },
                    ))
                    logger.info("auto_improver_skill_hot_reloaded",
                                skill_id=skill["id"], tool_count=len(shared._tools))
                else:
                    logger.warning("auto_improver_tool_reload_skipped_no_shared")
            except Exception as e:
                logger.warning("auto_improver_tool_reload_failed", error=str(e))

            logger.info("auto_improver_skill_created", skill_id=skill["id"], name=skill["name"])
            return {"type": "skill", "status": "created", "skill_id": skill["id"], "name": skill["name"]}
        except Exception as e:
            logger.error("auto_improver_skill_error", error=str(e))
            return {"type": "skill", "status": "error", "error": str(e)}

    async def _generate_patch(self, decision: dict, context: dict) -> dict[str, Any]:
        target = decision.get("target", "")
        if not target:
            return {"type": "patch", "status": "failed", "reason": "No target file specified"}

        target_path = Path(target)
        if not target_path.exists():
            return {"type": "patch", "status": "failed", "reason": f"Target not found: {target}"}

        try:
            original = target_path.read_text(encoding="utf-8")
            prompt = f"""\n你是一位代码工程师。请根据以下反思结果，修改文件 {target} 的代码。

反思结果:
{json.dumps(context, ensure_ascii=False, indent=2)}

当前文件内容:
```python
{original}
```

请输出完整的修改后文件内容（不要只输出 diff，输出完整代码），我会人工审核后再应用。
"""
            modified = await self.llm.complete([{"role": "user", "content": prompt}])
            modified = modified.strip().strip("`").replace("python", "").strip()

            proposal_id = f"proposal_{uuid.uuid4().hex[:8]}"
            proposal_path = self.proposals_dir / f"{proposal_id}.patch"

            patch_content = f"""--- {target}\n+++ {target}\n@@ -1,{len(original.splitlines())} +1,{len(modified.splitlines())} @@\n{modified}\n"""
            proposal_path.write_text(patch_content, encoding="utf-8")

            logger.info("auto_improver_patch_proposed", proposal_id=proposal_id, target=target)
            return {
                "type": "patch",
                "status": "proposed",
                "proposal_id": proposal_id,
                "target": target,
                "path": str(proposal_path),
            }
        except Exception as e:
            logger.error("auto_improver_patch_error", error=str(e))
            return {"type": "patch", "status": "error", "error": str(e)}

    def _auto_commit(self, message: str) -> None:
        """Create a git commit as a rollback point before auto-modification."""
        try:
            subprocess.run(["git", "add", "-A"], cwd=BASE_DIR, check=True, capture_output=True)
            subprocess.run(["git", "commit", "-m", message], cwd=BASE_DIR, check=False, capture_output=True)
        except Exception as e:
            logger.warning("auto_improver_git_commit_failed", error=str(e))
