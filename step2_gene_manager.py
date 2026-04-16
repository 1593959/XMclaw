"""Step 2: Enhance GeneManager with intent/regex matching and priority sorting."""
content = '''"""Gene manager: load and match behavior genes."""
import re
from xmclaw.memory.sqlite_store import SQLiteStore
from xmclaw.utils.paths import BASE_DIR


class GeneManager:
    def __init__(self, agent_id: str = "default"):
        self.agent_id = agent_id
        self.db = SQLiteStore(BASE_DIR / "shared" / "memory.db")

    def get_all(self) -> list[dict]:
        """Get all genes for this agent, sorted by priority (highest first)."""
        genes = self.db.get_genes(self.agent_id)
        return sorted(genes, key=lambda g: g.get("priority", 0), reverse=True)

    def match(self, user_input: str, intents: list[str] | None = None) -> list[dict]:
        """
        Match genes against user input using multiple strategies.
        
        Strategies (all checked, union of matches):
        1. Keyword match: gene.trigger appears in user_input
        2. Intent match: gene.intents overlaps with provided intents
        3. Regex match: gene.regex_pattern matches user_input
        
        Args:
            user_input: The user's message
            intents: Optional list of detected intents (e.g. ["repair", "code", "file"])
        
        Returns:
            List of matched genes, sorted by priority (highest first).
        """
        genes = self.get_all()
        matched = []
        input_lower = user_input.lower()

        for gene in genes:
            if not gene.get("enabled", True):
                continue

            # Strategy 1: keyword trigger
            trigger = gene.get("trigger", "").lower()
            keyword_hit = trigger and trigger in input_lower

            # Strategy 2: intent overlap
            gene_intents = gene.get("intents", [])
            intent_hit = intents and any(i in gene_intents for i in intents)

            # Strategy 3: regex pattern
            regex_pattern = gene.get("regex_pattern", "")
            regex_hit = False
            if regex_pattern:
                try:
                    regex_hit = bool(re.search(regex_pattern, user_input, re.IGNORECASE))
                except re.error:
                    pass

            if keyword_hit or intent_hit or regex_hit:
                matched.append(gene)

        return matched

    def get_gene(self, gene_id: str) -> dict | None:
        """Get a specific gene by ID."""
        for gene in self.get_all():
            if gene.get("gene_id") == gene_id:
                return gene
        return None

    def count(self) -> int:
        """Count total genes for this agent."""
        return len([g for g in self.get_all() if g.get("enabled", True)])
'''
with open(r"C:\Users\15978\Desktop\XMclaw\xmclaw\genes\manager.py", "w", encoding="utf-8") as f:
    f.write(content)
print("Step 2: GeneManager enhanced.")
