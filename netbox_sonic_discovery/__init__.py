"""NetBox plugin for automatic Dell SONiC device discovery via NAPALM."""

from netbox.plugins import PluginConfig


class SonicDiscoveryConfig(PluginConfig):
    name = "netbox_sonic_discovery"
    verbose_name = "SONiC Device Discovery"
    description = (
        "Auto-discover interfaces, IPs, cables, and facts "
        "from Dell SONiC switches via NAPALM RESTCONF"
    )
    version = "0.1.0"
    base_url = "sonic-discovery"
    min_version = "4.0.0"
    default_settings = {
        "create_missing_interface_templates": True,
        "sync_ip_addresses": True,
        "sync_cables_from_lldp": True,
    }


config = SonicDiscoveryConfig
