"""
Triggers an escalation workflow when a user reports that a previously fixed bug has re‑occurred, ensuring a deeper investigation and preventing endless re‑fix loops.
Auto-generated Gene for XMclaw.
"""
from xmclaw.genes.base import GeneBase

class Reopenedbugescalation(GeneBase):
    gene_id = "gene_06ff720b"
    name = "ReopenedBugEscalation"
    description = """Triggers an escalation workflow when a user reports that a previously fixed bug has re‑occurred, ensuring a deeper investigation and preventing endless re‑fix loops."""
    trigger = "{'type': 'user_report', 'condition': "(issue.status == 'reopened') OR (issue.keywords CONTAINS 'bug again' AND issue.was_fixed == true)"}"

    async def evaluate(self, context: dict) -> bool:
        """Return True if this gene should activate."""
        user_input = context.get("user_input", "")
        return self.trigger.lower() in user_input.lower()

    async def execute(self, context: dict) -> str:
        """Execute the gene's action."""
        issue = context.get("issue")
            if not issue:
                return "No issue data provided."
            # Determine if escalation should be triggered
            status = issue.get("status")
            keywords = issue.get("keywords", [])
            was_fixed = issue.get("was_fixed")
            if status == "reopened" or ("bug again" in keywords and was_fixed):
                # Execute escalation actions
                incident_result = await create_incident(priority="high")
                assign_result = await assign_to(role="senior_developer")
                notify_result = await notify_team(
                    channel="bug-alerts",
                    message="User reports the bug again after it was fixed. Immediate root‑cause analysis is required."
                )
                review_result = await schedule_review(review_type="root_cause_analysis")
                return (
                    f"Escalation triggered: {incident_result} | "
                    f"{assign_result} | {notify_result} | {review_result}"
                )
            else:
                return "No escalation needed."
        return "Gene ReopenedBugEscalation activated."
