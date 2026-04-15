"""Base class for executable genes."""
from abc import ABC, abstractmethod


class GeneBase(ABC):
    gene_id: str = ""
    name: str = ""
    description: str = ""
    trigger: str = ""

    @abstractmethod
    async def evaluate(self, context: dict) -> bool:
        pass

    @abstractmethod
    async def execute(self, context: dict) -> str:
        pass
