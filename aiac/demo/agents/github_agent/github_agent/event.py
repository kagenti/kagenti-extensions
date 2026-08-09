from abc import ABC, abstractmethod


class Event(ABC):
    @abstractmethod
    async def emit_event(self, message: str, final: bool = False) -> None:
        """Emit Event"""
        pass
