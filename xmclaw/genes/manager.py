"""Gene manager: load and match behavior genes."""
import json
import re
from xmclaw.memory.sqlite_store import SQLiteStore
from xmclaw.utils.paths import BASE_DIR


class GeneManager:
    # Known trigger types and their matching strategies
    TRIGGER_TYPES = frozenset({"keyword", "event", "regex", "intent"})

    def __init__(self, agent_id: str = "default"):
        self.agent_id = agent_id
        self.db = SQLiteStore(BASE_DIR / "shared" / "memory.db")

    def get_all(self) -> list[dict]:
        """Get all genes for this agent, sorted by priority (highest first)."""
        genes = self.db.get_genes(self.agent_id)
        return sorted(genes, key=lambda g: g.get("priority", 0), reverse=True)

    def match(self, user_input: str, intents: list[str] | None = None) -> list[dict]:
        """
        Match genes against user input using strategy inferred from trigger_type.

        trigger_type → strategy:
          keyword  gene.trigger (plaintext keyword) must appear in user_input
          regex    gene.trigger (regex pattern) must match user_input
          intent   gene.intents (JSON list) must overlap with provided intents
          event    gene.trigger is an event-type string, matched against intents
                   (kept for backward compat; treated like keyword if no intents)
          (none)   legacy fallback: try keyword first, then regex
        """
        genes = self.get_all()
        matched = []
        input_lower = user_input.lower()

        for gene in genes:
            if not gene.get("enabled", True):
                continue

            trigger_type = gene.get("trigger_type", "").lower() or "keyword"
            trigger = gene.get("trigger", "")

            hit = False

            if trigger_type == "keyword":
                hit = bool(trigger and trigger.lower() in input_lower)

            elif trigger_type == "regex":
                if trigger:
                    try:
                        hit = bool(re.search(trigger, user_input, re.IGNORECASE))
                    except re.error:
                        pass

            elif trigger_type == "intent":
                # intents stored as JSON list in DB
                raw_intents = gene.get("intents", "[]")
                if isinstance(raw_intents, str):
                    try:
                        gene_intents: list[str] = json.loads(raw_intents)
                    except json.JSONDecodeError:
                        gene_intents = []
                else:
                    gene_intents = list(raw_intents) if raw_intents else []
                hit = bool(intents) and any(i in gene_intents for i in intents)

            elif trigger_type == "event":
                # An "event" trigger fires when the provided intents list contains
                # the trigger value — used for programmatic event-driven activation.
                hit = bool(intents) and trigger in intents

            else:
                # Legacy fallback: try keyword, then regex, then intent
                if trigger and trigger.lower() in input_lower:
                    hit = True
                elif trigger:
                    try:
                        hit = bool(re.search(trigger, user_input, re.IGNORECASE))
                    except re.error:
                        pass
                # Intent check as last resort
                if not hit and intents:
                    raw_intents = gene.get("intents", "[]")
                    try:
                        gene_intents = json.loads(raw_intents) if isinstance(raw_intents, str) else list(raw_intents)
                    except (json.JSONDecodeError, TypeError):
                        gene_intents = []
                    hit = bool(any(i in gene_intents for i in intents))

            if hit:
                matched.append(gene)

        return matched

    def get_gene(self, gene_id: str) -> dict | None:
        """Get a specific gene by ID."""
        for gene in self.get_all():
            if gene.get("id") == gene_id:
                return gene
        return None

    def count(self) -> int:
        """Count total genes for this agent."""
        return len([g for g in self.get_all() if g.get("enabled", True)])
