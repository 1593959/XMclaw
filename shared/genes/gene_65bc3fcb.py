"""
This gene detects when a bug that was previously fixed is being reported or fixed again. It triggers a deeper investigation into why the fix didn't hold, identifying potential root causes such as incomplete fixes, regressions, or architectural issues that keep spawning the same bug.
Auto-generated Gene for XMclaw.
"""
from xmclaw.genes.base import GeneBase

class BugFixRegressionDetector(GeneBase):
    gene_id = "gene_65bc3fcb"
    name = "Bug Fix Regression Detector"
    description = """This gene detects when a bug that was previously fixed is being reported or fixed again. It triggers a deeper investigation into why the fix didn't hold, identifying potential root causes such as incomplete fixes, regressions, or architectural issues that keep spawning the same bug."""
    trigger = "{'event': 'bug_fix_reopened', 'conditions': ["bug.status == 'in_progress'", "bug.resolution == 'fixed'", 'bug.age_since_last_fix < 30_days'], 'description': "Activates when a bug marked as 'fixed' transitions back to 'open' or when a developer begins working on a bug with the same root cause within 30 days of a previous fix"}"

    async def evaluate(self, context: dict) -> bool:
        """Return True if this gene should activate."""
        user_input = context.get("user_input", "")
        return self.trigger.lower() in user_input.lower()

    async def execute(self, context: dict) -> str:
        """Execute the gene's action."""
        bug = context.get("bug", {})
        if not bug:
            return "No bug data found in context."
        
        # Verify trigger conditions
        if bug.get("status") != "in_progress":
            return "Bug status is not 'in_progress' – skipping regression detection."
        if bug.get("resolution") != "fixed":
            return "Bug resolution is not 'fixed' – skipping regression detection."
        
        age_since_fix = bug.get("age_since_last_fix")
        if age_since_fix is None or age_since_fix >= 30:
            return "Bug age since last fix exceeds 30 days – skipping regression detection."
        
        # --- Investigation workflow ---
        # 1. Tag the issue as 'Recurring Bug'
        await self.tag_issue(bug["id"], "Recurring Bug")
        
        # 2. Link to previous fix commits and pull requests
        previous_fixes = bug.get("previous_fixes", [])
        linked_resources = []
        for fix_info in previous_fixes:
            commit_url = fix_info.get("commit_url")
            pr_url = fix_info.get("pr_url")
            if commit_url:
                await self.link_issue_to_resource(bug["id"], commit_url)
                linked_resources.append(f"Commit: {commit_url}")
            if pr_url:
                await self.link_issue_to_resource(bug["id"], pr_url)
                linked_resources.append(f"PR: {pr_url}")
        
        # 3. Run regression analysis on affected code modules
        affected_modules = bug.get("affected_modules", [])
        regression_results = []
        for module in affected_modules:
            result = await self.run_regression_analysis(module)
            regression_results.append(f"{module}: {result}")
        
        # 4. Notify engineering lead for architectural review
        lead_email = bug.get("engineering_lead", "engineering_lead@example.com")
        notification_status = await self.notify_lead(lead_email, bug["id"], regression_results)
        
        # 5. Create technical‑debt ticket if root cause appears systemic
        technical_debt_ticket = None
        if bug.get("systemic", False):
            technical_debt_ticket = await self.create_technical_debt_ticket(
                title=f"TechDebt: Recurring bug {bug['id']}",
                description=f"Root cause: {bug.get('root_cause', 'Unknown')}",
                related_issue_id=bug["id"]
            )
        
        # Assemble output report
        report = {
            "type": "recurring_bug_report",
            "bug_id": bug["id"],
            "tag": "Recurring Bug",
            "linked_previous_fixes": linked_resources,
            "regression_analysis": regression_results,
            "notification_sent_to": lead_email,
            "notification_status": notification_status,
            "technical_debt_ticket": technical_debt_ticket,
            "escalate": True
        }
        import json
        return json.dumps(report, default=str)
        return "Gene Bug Fix Regression Detector activated."
