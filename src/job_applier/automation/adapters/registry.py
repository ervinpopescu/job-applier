from __future__ import annotations

from typing import Any

from job_applier.automation.adapters.ashby import AshbyAdapter
from job_applier.automation.adapters.base import BaseATSAdapter
from job_applier.automation.adapters.generic import GenericFormAdapter
from job_applier.automation.adapters.greenhouse import GreenhouseAdapter
from job_applier.automation.adapters.lever import LeverAdapter

REGISTERED_ADAPTER_CLASSES: list[type[BaseATSAdapter]] = [
    GreenhouseAdapter,
    LeverAdapter,
    AshbyAdapter,
]

ADAPTER_NAME_MAP: dict[str, type[BaseATSAdapter]] = {
    "greenhouse": GreenhouseAdapter,
    "lever": LeverAdapter,
    "ashby": AshbyAdapter,
    "generic": GenericFormAdapter,
}


def get_adapter_by_name(name: str) -> BaseATSAdapter:
    """Returns an instance of the adapter corresponding to the given name."""
    cls = ADAPTER_NAME_MAP.get(name.lower(), GenericFormAdapter)
    return cls()


def get_adapter_for_url(url: str, page: Any | None = None) -> BaseATSAdapter:
    """
    Evaluates registered adapters in order to detect platform match.
    Falls back to GenericFormAdapter if no specialized adapter claims the URL/page.
    """
    for cls in REGISTERED_ADAPTER_CLASSES:
        adapter = cls()
        if adapter.detect(url, page=page):
            return adapter

    return GenericFormAdapter()


def list_supported_adapters() -> list[dict[str, Any]]:
    """Returns metadata for all registered adapters."""
    result: list[dict[str, Any]] = []
    for name, cls in ADAPTER_NAME_MAP.items():
        inst = cls()
        meta = inst.get_metadata()
        result.append(
            {
                "name": meta.name,
                "version": meta.version,
                "can_submit": meta.can_submit,
                "display_name": meta.display_name,
            }
        )
    return result
