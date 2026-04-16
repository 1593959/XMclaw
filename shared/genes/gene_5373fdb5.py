"""
Skill that automatically diagnoses and corrects error 3 reported by users.
Auto-generated Gene for XMclaw.
"""
from xmclaw.genes.base import GeneBase

class Fixerror3skill(GeneBase):
    gene_id = "gene_5373fdb5"
    name = "FixError3Skill"
    description = """Skill that automatically diagnoses and corrects error 3 reported by users."""
    trigger = "error_3_reported"

    async def evaluate(self, context: dict) -> bool:
        """Return True if this gene should activate."""
        user_input = context.get("user_input", "")
        return self.trigger.lower() in user_input.lower()

    async def execute(self, context: dict) -> str:
        """Execute the gene's action."""
        import logging
        import traceback
        logger = logging.getLogger(__name__)
        logger.info("Attempting to fix error 3...")
        error_context = context.get('error')
        if error_context and error_context.get('code') == 3:
            try:
                # Example remediation steps for error 3
                # (replace with actual fix logic as needed)
                resource_id = error_context.get('resource_id')
                service_name = error_context.get('service_name')
        
                # Step 1: Reset the problematic resource
                reset_resource(resource_id)
        
                # Step 2: Clear any cached state related to the resource
                clear_cache(resource_id)
        
                # Step 3: Re‑initialize the affected service
                reinitialize_service(service_name)
        
                logger.info("Error 3 fixed successfully.")
                return {'status': 'fixed', 'message': 'Error 3 resolved'}
            except Exception as e:
                logger.error("Failed to fix error 3: %s", str(e))
                logger.debug(traceback.format_exc())
                return {'status': 'failed', 'error': str(e)}
        else:
            logger.warning("No error 3 detected in context.")
            return {'status': 'skipped', 'message': 'Error code not 3'}
        return "Gene FixError3Skill activated."
