"""
When a user reports that something is broken and mentions an error, this skill extracts the error identifier, looks up a known fix, and replies with instructions.
Auto-generated Gene for XMclaw.
"""
from xmclaw.genes.base import GeneBase

class Fixuserreportederror(GeneBase):
    gene_id = "gene_a217eb4e"
    name = "FixUserReportedError"
    description = """When a user reports that something is broken and mentions an error, this skill extracts the error identifier, looks up a known fix, and replies with instructions."""
    trigger = "The skill is triggered when the user's message contains the words 'broken' and 'error' (case‑insensitive)."

    async def evaluate(self, context: dict) -> bool:
        """Return True if this gene should activate."""
        user_input = context.get("user_input", "")
        return self.trigger.lower() in user_input.lower()

    async def execute(self, context: dict) -> str:
        """Execute the gene's action."""
        match = re.search(r'error\s+(\d+)', user_message, re.IGNORECASE)
        if match:
            error_id = match.group(1)
            known_fixes = {
                "1": "Please ensure the database connection string is correct and try again.",
                "2": "Restart the service and check the logs for further details.",
                "3": "Clear the cache and reload the page."
            }
            response = known_fixes.get(error_id, "I'm sorry, I don't have a known fix for error " + error_id + ". Please contact support.")
        else:
            response = "I couldn't identify a specific error in your message. Could you provide more details?"
        return {"message": response, "action": "reply"}
        return "Gene FixUserReportedError activated."
