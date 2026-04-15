"""
Monitors user messages for repeated error reports and triggers proactive support action.
Auto-generated Gene for XMclaw.
"""
from xmclaw.genes.base import GeneBase

class ErrorReportFrequencyDetector(GeneBase):
    gene_id = "gene_ed9ecb1c"
    name = "Error Report Frequency Detector"
    description = """Monitors user messages for repeated error reports and triggers proactive support action."""
    trigger = "{"type": "message_pattern_match", "keywords": ["broken", "error", "fix"]}"

    async def evaluate(self, context: dict) -> bool:
        """Return True if this gene should activate."""
        user_input = context.get("user_input", "")
        return self.trigger.lower() in user_input.lower()

    async def execute(self, context: dict) -> str:
        """Execute the gene's action."""
        user_input = context.get("user_input", "").lower()
            keywords = ["broken", "error", "fix"]
            if any(keyword in user_input for keyword in keywords):
                # Increment the count of error reports for this session
                error_count = context.get("error_report_count", 0) + 1
                context["error_report_count"] = error_count
                # Trigger proactive support if the count reaches the threshold
                if error_count >= 2:
                    await self.notify_support(context)
                    return "Repeated error reports detected. Support has been notified."
                else:
                    return "Error report logged."
            else:
                return "No error keywords detected."
        return "Gene Error Report Frequency Detector activated."
