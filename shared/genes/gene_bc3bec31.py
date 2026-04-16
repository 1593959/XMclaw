"""
A skill that automatically detects and attempts to resolve error 0 reported by the user.
Auto-generated Gene for XMclaw.
"""
from xmclaw.genes.base import GeneBase

class Fixerrorzero(GeneBase):
    gene_id = "gene_bc3bec31"
    name = "FixErrorZero"
    description = """A skill that automatically detects and attempts to resolve error 0 reported by the user."""
    trigger = "User says "this is broken, please fix error 0" or mentions "error 0""

    async def evaluate(self, context: dict) -> bool:
        """Return True if this gene should activate."""
        user_input = context.get("user_input", "")
        return self.trigger.lower() in user_input.lower()

    async def execute(self, context: dict) -> str:
        """Execute the gene's action."""
        logger.info("Received request to fix error 0")
        # Retrieve any extra context about the error
        error_info = context.get("error_info", {})
        if error_info.get("code") != 0:
            # If the error code is not explicitly 0, treat it as the reported error
            error_info["code"] = 0
        # Attempt recovery steps (example: restart a specific service)
        try:
            service = self._service_manager.get_service("example_service")
            if service is None:
                logger.warning("Service 'example_service' not found")
                response_msg = "Could not locate the service to restart."
            else:
                success = service.restart()
                if success:
                    logger.info("Service restarted successfully for error 0")
                    response_msg = "Error 0 has been resolved. The service has been restarted."
                else:
                    logger.warning("Service restart returned failure for error 0")
                    response_msg = "Unable to automatically fix error 0. Please contact support."
        except Exception as e:
            logger.exception("Exception while fixing error 0")
            response_msg = "An unexpected error occurred while fixing error 0."
        # Return the response message to the caller
        return {"message": response_msg}
        return "Gene FixErrorZero activated."
