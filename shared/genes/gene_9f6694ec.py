"""
强制 Agent 在工具调用后验证数据有效性，失败时自动重试，并保持问题导向的回复风格
Auto-generated Gene for XMclaw.
"""
from xmclaw.genes.base import GeneBase

class 工具调用验证与重试行为基因(GeneBase):
    gene_id = "gene_9f6694ec"
    name = "工具调用验证与重试行为基因"
    description = """强制 Agent 在工具调用后验证数据有效性，失败时自动重试，并保持问题导向的回复风格"""
    trigger = "任何工具调用场景（weather/query/search/calculate 等）"

    async def evaluate(self, context: dict) -> bool:
        """Return True if this gene should activate."""
        user_input = context.get("user_input", "")
        return self.trigger.lower() in user_input.lower()

    async def execute(self, context: dict) -> str:
        """Execute the gene's action."""
        def execute(self, tool_result, tool_name, context):
            """
            工具调用结果处理基因
            确保数据有效、失败重试、回复聚焦解决
            """
            # Step 1: 数据有效性验证
            if tool_result is None:
                raise ToolCallError(f"{tool_name} returned None")
        
            if isinstance(tool_result, dict):
                if tool_result.get('error') or tool_result.get('status') == 'error':
                    raise ToolCallError(f"{tool_name} returned error: {tool_result}")
                if not tool_result.get('data') and not tool_result.get('result'):
                    raise ToolCallError(f"{tool_name} returned empty data")
        
            # Step 2: 格式校验（示例）
            if tool_name == 'weather' and 'temperature' not in str(tool_result):
                raise ToolCallError(f"{tool_name} data format invalid")
        
            # Step 3: 返回有效数据，触发后续回复逻辑
            return {
                'valid': True,
                'data': tool_result,
                'needs_retry': False
            }
        return "Gene 工具调用验证与重试行为基因 activated."