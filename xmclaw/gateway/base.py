"""Gateway base abstraction."""
from abc import ABC, abstractmethod
from typing import AsyncIterator


class Gateway(ABC):
    @abstractmethod
    async def connect(self) -> None:
        pass

    @abstractmethod
    async def send(self, message: str) -> None:
        pass

    @abstractmethod
    async def receive_stream(self) -> AsyncIterator[str]:
        pass

    @abstractmethod
    async def disconnect(self) -> None:
        pass
