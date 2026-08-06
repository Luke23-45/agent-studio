import structlog
from abc import ABC, abstractmethod
from typing import Optional

from .health import HealthCheckable, HealthComponent, HealthStatus, HealthReport

logger = structlog.get_logger(__name__)


class ServiceLifecycle(ABC):
    @abstractmethod
    async def initialize(self) -> None:
        ...

    @abstractmethod
    async def close(self) -> None:
        ...

    async def __aenter__(self):
        await self.initialize()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self.close()


class ManagedService(ServiceLifecycle, HealthCheckable):
    def __init__(self, name: str):
        self.name = name
        self._initialized = False

    async def initialize(self) -> None:
        if self._initialized:
            return
        await self._do_initialize()
        self._initialized = True
        logger.info("service_initialized", service=self.name)

    async def close(self) -> None:
        if not self._initialized:
            return
        await self._do_close()
        self._initialized = False
        logger.info("service_closed", service=self.name)

    @abstractmethod
    async def _do_initialize(self) -> None:
        ...

    @abstractmethod
    async def _do_close(self) -> None:
        ...

    async def health_check(self) -> HealthComponent:
        if not self._initialized:
            return HealthComponent(
                name=self.name,
                status=HealthStatus.UNHEALTHY,
                message="Service not initialized",
            )
        try:
            return await self._do_health_check()
        except Exception as e:
            return HealthComponent(
                name=self.name,
                status=HealthStatus.UNHEALTHY,
                message=str(e),
            )

    async def _do_health_check(self) -> HealthComponent:
        return HealthComponent(name=self.name, status=HealthStatus.HEALTHY)
