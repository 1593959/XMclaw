"""Gene manager: load and match behavior genes."""
from xmclaw.memory.sqlite_store import SQLiteStore
from xmclaw.utils.paths import BASE_DIR


class GeneManager:
    def __init__(self, agent_id: str = "default"):
        self.agent_id = agent_id
        self.db = SQLiteStore(BASE_DIR / "shared" / "memory.db")

    def get_all(self) -> list[dict]:
        return self.db.get_genes(self.agent_id)

    def match(self, user_input: str) -> list[dict]:
        """Match genes against user input."""
        genes = self.get_all()
        matched = []
        for gene in genes:
            trigger = gene.get("trigger", "").lower()
            if trigger and trigger in user_input.lower():
                matched.append(gene)
        return matched
