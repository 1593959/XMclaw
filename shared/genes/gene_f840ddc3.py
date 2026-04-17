"""
确保每个工具调用都必须完成：识别意图→调用工具→解析结果→格式化回答→用户可见输出的完整流程。
Auto-generated Gene for XMclaw.
"""
from xmclaw.genes.base import GeneBase

class 工具调用闭环响应基因(GeneBase):
    gene_id = "gene_f840ddc3"
    name = "工具调用闭环响应基因"
    description = """确保每个工具调用都必须完成：识别意图→调用工具→解析结果→格式化回答→用户可见输出的完整流程。"""
    trigger = "任何工具调用(tool_call)指令后"

    async def evaluate(self, context: dict) -> bool:
        """Return True if this gene should activate."""
        user_input = context.get("user_input", "")
        return self.trigger.lower() in user_input.lower()

    async def execute(self, context: dict) -> str:
        """Execute the gene's action."""
        def execute(tool_call_result, original_intent):
            """确保工具调用后生成用户可见的完整回答"""
            # 1. 检查工具调用结果
            if tool_call_result is None:
                return {
                    "status": "error",
                    "response": "抱歉，查询失败，请稍后重试。"
                }
        
            # 2. 解析工具返回的原始数据
            parsed_data = parse_tool_result(tool_call_result)
        
            # 3. 格式化用户可读的答案
            formatted_response = format_for_user(parsed_data, original_intent)
        
            # 4. 返回完整响应，确保用户可直接获取答案
            return {
                "status": "success",
                "response": formatted_response,
                "data": parsed_data
            }
        
        def parse_tool_result(result):
            """根据工具类型解析结果"""
            if 'error' in result:
                raise ToolExecutionError(result['error'])
            return result.get('data', result)
        
        def format_for_user(data, intent):
            """将结构化数据转换为用户友好的自然语言回答"""
            if intent.get('type') == 'weather':
                return f"{data['location']}当前天气：{data['description']}，温度{data['temp']}°C"
            elif intent.get('type') == 'search':
                return f"根据搜索结果：{data['summary'] if 'summary' in data else data}"
            return str(data)
        return "Gene 工具调用闭环响应基因 activated."