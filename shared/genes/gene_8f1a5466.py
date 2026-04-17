"""
确保工具调用→结果解析→用户回复的完整闭环，根据问题复杂度自适应调整信息详细度，包含错误处理和降级策略
Auto-generated Gene for XMclaw.
"""
from xmclaw.genes.base import GeneBase

class 结构化工具使用与结果闭环基因(GeneBase):
    gene_id = "gene_8f1a5466"
    name = "结构化工具使用与结果闭环基因"
    description = """确保工具调用→结果解析→用户回复的完整闭环，根据问题复杂度自适应调整信息详细度，包含错误处理和降级策略"""
    trigger = "天气、温度、气候、预报、明天、后天、未来几天"

    async def evaluate(self, context: dict) -> bool:
        """Return True if this gene should activate."""
        user_input = context.get("user_input", "")
        return self.trigger.lower() in user_input.lower()

    async def execute(self, context: dict) -> str:
        """Execute the gene's action."""
        def execute(self, user_input, context):
            # Step 1: 意图分析与工具选择
            query_type = self.analyze_intent(user_input)  # 天气查询
        
            if query_type == 'weather':
                # Step 2: 参数优化 - 使用专用API + 具体日期
                location = self.extract_location(user_input)
                date = self.resolve_date(user_input)  # 转换为具体日期如'2025-01-26'
        
                try:
                    # 优先使用专用天气API
                    result = self.call_tool('weather_api', location=location, date=date)
                except Exception as e:
                    # Fallback机制
                    result = self.call_tool('web_search', 
                        query=f'{location} {date} 天气预报')
        
                # Step 3: 结果处理与格式化
                if result.get('success'):
                    data = result.get('data', {})
                    # Step 4: 根据复杂度自适应回复
                    if self.is_simple_query(user_input):
                        reply = f"{data.get('location', '该地')}今天{data.get('date', '')}：{data.get('temp', '?')}°C，{data.get('condition', '未知天气')}"
                    else:
                        reply = self.format_detailed_weather(data)
                else:
                    reply = f"抱歉，查询失败：{result.get('error', '未知错误')}。您可以换个方式描述或稍后重试。"
        
                return self.generate_response(reply)
        return "Gene 结构化工具使用与结果闭环基因 activated."