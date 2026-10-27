"""Connector layer: capabilities (erp, email...) served by interchangeable drivers, MCP first. See docs/TEAM-CONNECTORS.md."""
from .descriptor import DescriptorRejected, validate_descriptor
from .registry import Capability, ConnectorRegistry, Driver, LocalRouter

__all__ = ["Capability", "ConnectorRegistry", "DescriptorRejected", "Driver", "LocalRouter", "validate_descriptor"]
