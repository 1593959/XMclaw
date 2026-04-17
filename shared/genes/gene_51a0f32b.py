"""
规范 Agent 在执行信息查询时的行为模式：先告知用户操作意图、优先使用专用 API、适当增加结果数量以提高准确性
Auto-generated Gene for XMclaw.
"""
from xmclaw.genes.base import GeneBase

class 专业信息查询行为准则(GeneBase):
    gene_id = "gene_51a0f32b"
    name = "专业信息查询行为准则"
    description = """规范 Agent 在执行信息查询时的行为模式：先告知用户操作意图、优先使用专用 API、适当增加结果数量以提高准确性"""
    trigger = "天气、温度、气候、降水、风力、湿度、空气质量"

    async def evaluate(self, context: dict) -> bool:
        """Return True if this gene should activate."""
        user_input = context.get("user_input", "")
        return self.trigger.lower() in user_input.lower()

    async def execute(self, context: dict) -> str:
        """Execute the gene's action."""
        def execute(context):
            query = context.get('query', '')
            # 触发词检测：中文天气相关词汇
            weather_keywords = ['天气', '温度', '气候', '降水', '风力', '湿度', '气温', '降雨', '空气', 'PM']
        
            if any(kw in query for kw in weather_keywords):
                # 1. 工具调用前先告知用户
                user_message = '正在为您查询天气信息，请稍候...'
                context.setdefault('messages', []).append({'role': 'assistant', 'content': user_message})
        
                # 2. 优先选择专用天气 API 而非通用搜索
                if 'weather_api' in context.get('available_tools', []):
                    tool_name = 'weather_api'
                else:
                    tool_name = 'search'
                    # 3. 适当增加 max_results 以提高准确性
                    context['tool_params'] = {
                        'max_results': 6
                    }
        
                # 4. 传递操作意图给工具层
                context['operation_intent'] = f'查询{query}相关的天气数据'
        
                return {'action': 'call_tool', 'tool': tool_name, 'proceed': True}
        
            return {'action': 'continue', 'proceed': False}
        return "Gene 专业信息查询行为准则 activated."