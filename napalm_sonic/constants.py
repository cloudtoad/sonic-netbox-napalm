"""Constants for the Dell Enterprise SONiC NAPALM driver."""

# RESTCONF base path
RESTCONF_ROOT = "/restconf/data"

# Default RESTCONF headers
RESTCONF_HEADERS = {
    "Accept": "application/yang-data+json",
    "Content-Type": "application/yang-data+json",
}

# OpenConfig YANG paths
OC_SYSTEM_STATE = "openconfig-system:system/state"
OC_PLATFORM_COMPONENTS = "openconfig-platform:components"
OC_PLATFORM_COMPONENT = "openconfig-platform:components/component={name}"
OC_SOFTWARE_MODULE = "openconfig-platform:components/component=SoftwareModule"
OC_SYSTEM_EEPROM = "openconfig-platform:components/component=System%20Eeprom"
OC_INTERFACES = "openconfig-interfaces:interfaces"
OC_INTERFACE = "openconfig-interfaces:interfaces/interface={name}"
OC_INTERFACE_COUNTERS = (
    "openconfig-interfaces:interfaces/interface={name}/state/counters"
)
OC_INTERFACE_IPV4_ADDRS = (
    "openconfig-interfaces:interfaces/interface={name}"
    "/subinterfaces/subinterface=0/openconfig-if-ip:ipv4/addresses"
)
OC_INTERFACE_IPV6_ADDRS = (
    "openconfig-interfaces:interfaces/interface={name}"
    "/subinterfaces/subinterface=0/openconfig-if-ip:ipv6/addresses"
)
OC_INTERFACE_IPV4_NEIGHBORS = (
    "openconfig-interfaces:interfaces/interface={name}"
    "/subinterfaces/subinterface=0/openconfig-if-ip:ipv4/neighbors"
)
OC_INTERFACE_IPV6_NEIGHBORS = (
    "openconfig-interfaces:interfaces/interface={name}"
    "/subinterfaces/subinterface=0/openconfig-if-ip:ipv6/neighbors"
)
OC_LLDP_INTERFACES = "openconfig-lldp:lldp/interfaces"
OC_NETWORK_INSTANCES = "openconfig-network-instance:network-instances"
OC_NETWORK_INSTANCE = (
    "openconfig-network-instance:network-instances/network-instance={name}"
)
OC_BGP_NEIGHBORS = (
    "openconfig-network-instance:network-instances"
    "/network-instance={vrf}/protocols/protocol=BGP,bgp/bgp/neighbors"
)
OC_BGP_GLOBAL = (
    "openconfig-network-instance:network-instances"
    "/network-instance={vrf}/protocols/protocol=BGP,bgp/bgp/global"
)
OC_MAC_TABLE = (
    "openconfig-network-instance:network-instances"
    "/network-instance={name}/fdb/mac-table/entries"
)
OC_AFTS = (
    "openconfig-network-instance:network-instances"
    "/network-instance={vrf}/afts"
)
OC_ACL = "openconfig-acl:acl"
OC_AAA = "openconfig-system:system/aaa"
OC_NTP = "openconfig-system:system/ntp"
OC_IP_SLA = "openconfig-ip-sla:ip-slas"
SONIC_IP_SLA = "sonic-ip-sla:sonic-ip-sla"

# Speed mapping from OpenConfig enum to Mbps
SPEED_MAP = {
    "openconfig-if-ethernet:SPEED_10MB": 10.0,
    "openconfig-if-ethernet:SPEED_100MB": 100.0,
    "openconfig-if-ethernet:SPEED_1GB": 1000.0,
    "openconfig-if-ethernet:SPEED_2500MB": 2500.0,
    "openconfig-if-ethernet:SPEED_5GB": 5000.0,
    "openconfig-if-ethernet:SPEED_10GB": 10000.0,
    "openconfig-if-ethernet:SPEED_25GB": 25000.0,
    "openconfig-if-ethernet:SPEED_40GB": 40000.0,
    "openconfig-if-ethernet:SPEED_50GB": 50000.0,
    "openconfig-if-ethernet:SPEED_100GB": 100000.0,
    "openconfig-if-ethernet:SPEED_200GB": 200000.0,
    "openconfig-if-ethernet:SPEED_400GB": 400000.0,
    "openconfig-if-ethernet:SPEED_UNKNOWN": 0.0,
}
