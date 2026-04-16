"""
Skill that automatically resolves error 1 when a user reports a broken component.
Auto-generated Gene for XMclaw.
"""
from xmclaw.genes.base import GeneBase

class Autofixerrorone(GeneBase):
    gene_id = "gene_29e9b555"
    name = "AutoFixErrorOne"
    description = """Skill that automatically resolves error 1 when a user reports a broken component."""
    trigger = "User says 'this is broken, please fix error 1'"

    async def evaluate(self, context: dict) -> bool:
        """Return True if this gene should activate."""
        user_input = context.get("user_input", "")
        return self.trigger.lower() in user_input.lower()

    async def execute(self, context: dict) -> str:
        """Execute the gene's action."""
        # Acknowledge the issue
        context['output'].append("I'm sorry you're experiencing error 1. Let me investigate.")
        # Identify the broken component
        broken_component = self._find_component_by_error_code(context, "error_1")
        if not broken_component:
            context['output'].append("Could not locate the component associated with error 1.")
            return
        # Attempt to reset the component
        try:
            broken_component.reset()
            context['output'].append("Component has been reset successfully.")
        except Exception as e:
            context['output'].append(f"Failed to reset component: {e}")
        return "Gene AutoFixErrorOne activated."
