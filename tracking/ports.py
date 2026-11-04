"""Injection boundaries. No concrete database or HTTP implementations."""
from typing import Protocol
from .service_types import ShipmentSnapshot, Attempt, Response


class ReadOnlyRepository(Protocol):
    def by_id(self, local_id: int) -> ShipmentSnapshot | None: ...
    def by_customer_number(self, number: str) -> tuple[ShipmentSnapshot, ...]: ...


class ResponseProvider(Protocol):
    def fetch(self, attempt: Attempt) -> Response: ...
