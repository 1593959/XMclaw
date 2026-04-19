"""Environment abstraction for tool execution."""
from abc import ABC, abstractmethod
from typing import Any


class Environment(ABC):
    @abstractmethod
    async def execute(self, command: str, **kwargs) -> dict[str, Any]:
        pass
