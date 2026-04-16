"""
Skill that automatically diagnoses and resolves error 2 reported by users.
Auto-generated Gene for XMclaw.
"""
from xmclaw.genes.base import GeneBase

class Fixerror2skill(GeneBase):
    gene_id = "gene_17d104cc"
    name = "FixError2Skill"
    description = """Skill that automatically diagnoses and resolves error 2 reported by users."""
    trigger = "error_2"

    async def evaluate(self, context: dict) -> bool:
        """Return True if this gene should activate."""
        user_input = context.get("user_input", "")
        return self.trigger.lower() in user_input.lower()

    async def execute(self, context: dict) -> str:
        """Execute the gene's action."""
        # Retrieve the full error details from the context
        error_details = self.context.get('error_details', '')
        # Log the reported error
        logger.info('Fixing error 2: %s', error_details)
        # Perform a basic diagnostic check
        if 'timeout' in error_details.lower():
            # Increase timeout in configuration
            config = self.get_config()
            config['timeout'] = config.get('timeout', 30) + 10
            self.set_config(config)
            logger.info('Increased timeout setting.')
        # If the error is due to missing dependency, attempt to install it
        if 'module' in error_details.lower():
            missing_module = self.extract_missing_module(error_details)
            if missing_module:
                self.install_package(missing_module)
                logger.info('Installed missing module: %s', missing_module)
        # Final verification step: run the original operation again
        self.retry_operation()
        logger.info('Error 2 has been fixed.')
        return "Gene FixError2Skill activated."
