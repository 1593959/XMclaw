# Drop custom LLM provider plugins here
# Each .py file defining LLMProviderPlugin subclass is auto-loaded.
# Example (plugins/llm/ollama.py):
#   from xmclaw.llm.router import LLMProviderPlugin
#   class OllamaProvider(LLMProviderPlugin):
#       name = 'ollama'
#       supports_embeddings = True
#       async def stream(self, messages, tools=None): ...
