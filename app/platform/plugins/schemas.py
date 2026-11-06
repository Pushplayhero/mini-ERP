"""platform.plugins Pydantic schemas — ADR-010 / Week 10 Slice B.

Defines API DTOs for tenant plugin management (/api/v1/plugins):
- `CompanyPluginUpdate`: Partial mutation payload (extra="forbid", at least one field non-None).
- `PluginDependencyRead`: Inter-plugin prerequisite metadata.
- `CompanyPluginRead`: Complete metadata and effective tenant enablement/configuration.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, model_validator


class CompanyPluginUpdate(BaseModel):
    """Request payload for updating tenant plugin state/configuration."""

    model_config = ConfigDict(extra="forbid")

    is_enabled: bool | None = None
    config: dict[str, Any] | None = None

    @model_validator(mode="after")
    def validate_at_least_one_field(self) -> CompanyPluginUpdate:
        if self.is_enabled is None and self.config is None:
            raise ValueError("At least one of 'is_enabled' or 'config' must be provided")
        return self


class PluginDependencyRead(BaseModel):
    """Metadata for an inter-plugin dependency."""

    model_config = ConfigDict(extra="forbid")

    plugin_name: str
    min_version: str


class CompanyPluginRead(BaseModel):
    """Read representation of a plugin with its effective tenant state."""

    model_config = ConfigDict(extra="forbid")

    plugin_name: str
    version: str
    title: str
    description: str
    default_enabled: bool
    requires: list[PluginDependencyRead]
    is_enabled: bool
    config: dict[str, Any]
    is_customized: bool
