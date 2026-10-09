"""Provider registry (DSS-CTR-013-REQ-001, REQ-003).

Registration is an in-memory runtime operation only. The registry has no
database handle and never creates or modifies canonical domain records.
"""

from __future__ import annotations

from .errors import ProviderAlreadyRegistered, UnknownProvider
from .models import ProviderDescriptor, ProviderId
from .provider import DeviceProvider
from .runtime import ProviderRuntime
from .support import Clock, IdGenerator, random_id_generator, utc_now


class ProviderRegistry:
    def __init__(
        self,
        *,
        clock: Clock = utc_now,
        id_generator: IdGenerator = random_id_generator,
    ) -> None:
        self._clock = clock
        self._ids = id_generator
        self._runtimes: dict[ProviderId, ProviderRuntime] = {}

    def register(self, provider: DeviceProvider) -> ProviderRuntime:
        provider_id = provider.descriptor.provider_id
        if provider_id in self._runtimes:
            raise ProviderAlreadyRegistered(f"Provider '{provider_id}' is already registered.")
        runtime = ProviderRuntime(provider, clock=self._clock, id_generator=self._ids)
        self._runtimes[provider_id] = runtime
        return runtime

    def get(self, provider_id: ProviderId) -> ProviderRuntime:
        try:
            return self._runtimes[provider_id]
        except KeyError:
            raise UnknownProvider(f"Unknown Provider: {provider_id}") from None

    def list_descriptors(self) -> list[ProviderDescriptor]:
        return [self._runtimes[pid].descriptor for pid in sorted(self._runtimes)]
