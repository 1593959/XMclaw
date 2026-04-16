"""
Skill that automatically resolves error code 3 reported by the user, diagnosing the cause and applying the appropriate fix.
Auto-generated Gene for XMclaw.
"""
from xmclaw.genes.base import GeneBase

class Fixerror3skill(GeneBase):
    gene_id = "gene_d21ea948"
    name = "FixError3Skill"
    description = """Skill that automatically resolves error code 3 reported by the user, diagnosing the cause and applying the appropriate fix."""
    trigger = "User says: 'this is broken, please fix error 3'"

    async def evaluate(self, context: dict) -> bool:
        """Return True if this gene should activate."""
        user_input = context.get("user_input", "")
        return self.trigger.lower() in user_input.lower()

    async def execute(self, context: dict) -> str:
        """Execute the gene's action."""
        error_info = context.get('error_info', {})
        if error_info.get('code') == 3:
            cause = error_info.get('message', 'Unknown cause')
            logger.error('Error 3 detected: %s', cause)
            if 'timeout' in cause.lower():
                config = context.get('config', {})
                config['timeout'] = config.get('timeout', 30) * 2
                context['config'] = config
                logger.info('Timeout increased to %s seconds', config['timeout'])
            else:
                component = context.get('component', None)
                if component:
                    component.reset()
                    logger.info('Component reset performed.')
            return {'status': 'fixed', 'error_code': 3}
        else:
            logger.warning('No error 3 found in context.')
            return {'status': 'no_action'}
        return "Gene FixError3Skill activated."
