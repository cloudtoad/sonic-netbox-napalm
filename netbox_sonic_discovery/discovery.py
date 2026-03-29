"""Core discovery logic — all NAPALM interaction and NetBox object creation."""

import logging
import re

import napalm
from dcim.choices import InterfaceTypeChoices
from dcim.models import Cable, Device, DeviceType, Interface, InterfaceTemplate, MACAddress
from django.contrib.contenttypes.models import ContentType
from ipam.choices import PrefixStatusChoices, VLANStatusChoices
from ipam.models import ASN, IPAddress, Prefix, RIR, Role, RouteTarget, VLAN, VRF
import ipaddress
from vpn.choices import L2VPNTypeChoices
from vpn.models import L2VPN, L2VPNTermination
from netbox.plugins.utils import get_plugin_config

logger = logging.getLogger(__name__)

# Map NAPALM speed (Mbps float) to NetBox interface type
SPEED_TO_TYPE = {
    10: InterfaceTypeChoices.TYPE_1GE_FIXED,
    100: InterfaceTypeChoices.TYPE_1GE_FIXED,
    1000: InterfaceTypeChoices.TYPE_1GE_FIXED,
    2500: InterfaceTypeChoices.TYPE_2GE_FIXED,
    5000: InterfaceTypeChoices.TYPE_5GE_FIXED,
    10000: InterfaceTypeChoices.TYPE_10GE_SFP_PLUS,
    25000: InterfaceTypeChoices.TYPE_25GE_SFP28,
    40000: InterfaceTypeChoices.TYPE_40GE_QSFP_PLUS,
    50000: InterfaceTypeChoices.TYPE_50GE_QSFP28,
    100000: InterfaceTypeChoices.TYPE_100GE_QSFP28,
    200000: InterfaceTypeChoices.TYPE_200GE_QSFP56,
    400000: InterfaceTypeChoices.TYPE_400GE_OSFP,
}


def _classify_interface(name, speed):
    """Return (netbox_type, mgmt_only) for a given interface name and speed."""
    lower = name.lower()
    if lower.startswith("management") or lower == "eth0":
        return InterfaceTypeChoices.TYPE_1GE_FIXED, True
    if lower.startswith("loopback") or lower.startswith("vlan") or lower.startswith("vtep"):
        return InterfaceTypeChoices.TYPE_VIRTUAL, False
    if lower.startswith("portchannel"):
        return InterfaceTypeChoices.TYPE_LAG, False
    # Physical interface — map from speed
    itype = SPEED_TO_TYPE.get(int(speed), InterfaceTypeChoices.TYPE_OTHER)
    return itype, False


def get_napalm_driver(device):
    """Return an open NAPALM driver instance for a Device."""
    from netbox_napalm_plugin.models import NapalmPlatformConfig

    if not device.platform:
        raise ValueError(f"{device.name}: no platform assigned")
    if not device.primary_ip:
        raise ValueError(f"{device.name}: no primary IP assigned")

    try:
        napalm_config = NapalmPlatformConfig.objects.get(platform=device.platform)
    except NapalmPlatformConfig.DoesNotExist:
        raise ValueError(f"{device.name}: platform {device.platform} has no NAPALM config")

    driver_cls = napalm.get_network_driver(napalm_config.napalm_driver)
    host = str(device.primary_ip.address.ip)
    username = get_plugin_config("netbox_napalm_plugin", "NAPALM_USERNAME")
    password = get_plugin_config("netbox_napalm_plugin", "NAPALM_PASSWORD")
    timeout = get_plugin_config("netbox_napalm_plugin", "NAPALM_TIMEOUT")
    optional_args = dict(get_plugin_config("netbox_napalm_plugin", "NAPALM_ARGS") or {})
    if napalm_config.napalm_args:
        optional_args.update(napalm_config.napalm_args)

    driver = driver_cls(
        hostname=host,
        username=username,
        password=password,
        timeout=timeout,
        optional_args=optional_args,
    )
    driver.open()
    return driver


def sync_interfaces(device, driver, log_fn=None):
    """Discover interfaces from NAPALM and create/update in NetBox.

    Returns dict with counts: {created, updated, skipped}.
    """
    log = log_fn or logger.info
    napalm_ifaces = driver.get_interfaces()
    existing = {iface.name: iface for iface in Interface.objects.filter(device=device)}

    # First pass: create/update all interfaces
    created = updated = skipped = 0
    for name, data in napalm_ifaces.items():
        itype, mgmt_only = _classify_interface(name, data.get("speed", 0))

        if name in existing:
            iface = existing[name]
            changed = False
            if data.get("description") and iface.description != data["description"]:
                iface.description = data["description"]
                changed = True
            if data.get("mtu") and iface.mtu != data["mtu"]:
                iface.mtu = data["mtu"]
                changed = True
            new_speed = int(data.get("speed", 0)) * 1000  # Mbps -> Kbps
            if new_speed and iface.speed != new_speed:
                iface.speed = new_speed
                changed = True
            if iface.enabled != data.get("is_enabled", True):
                iface.enabled = data.get("is_enabled", True)
                changed = True
            if changed:
                iface.save()
                updated += 1
            else:
                skipped += 1
        else:
            Interface.objects.create(
                device=device,
                name=name,
                type=itype,
                enabled=data.get("is_enabled", True),
                mtu=data.get("mtu") or None,
                speed=int(data.get("speed", 0)) * 1000 or None,
                description=data.get("description", ""),
                mgmt_only=mgmt_only,
            )
            created += 1
            log(f"  Created interface {name}")

    # Second pass: set parent for breakout interfaces (e.g. Eth1/49/1 -> Eth1/49)
    # Refresh from DB to include newly created interfaces
    all_ifaces = {iface.name: iface for iface in Interface.objects.filter(device=device)}
    breakout_count = 0
    for name, iface in all_ifaces.items():
        # Detect breakout pattern: Eth1/X/Y where Y is the child index
        m = re.match(r"(Eth\d+/\d+)/\d+$", name)
        if not m:
            continue
        parent_name = m.group(1)
        parent_iface = all_ifaces.get(parent_name)
        if parent_iface and iface.parent_id != parent_iface.pk:
            iface.parent = parent_iface
            iface.save()
            breakout_count += 1

    if breakout_count:
        log(f"  Set parent on {breakout_count} breakout interfaces")

    # Third pass: sync MAC addresses from NAPALM data
    iface_ct = ContentType.objects.get_for_model(Interface)
    mac_count = 0
    for name, data in napalm_ifaces.items():
        mac_str = data.get("mac_address", "")
        if not mac_str or mac_str == "00:00:00:00:00:00":
            continue
        iface = all_ifaces.get(name)
        if not iface:
            continue
        # Skip if already has a primary MAC
        if iface.primary_mac_address_id:
            continue
        # Find or create the MACAddress object
        mac_obj, mac_created = MACAddress.objects.get_or_create(
            mac_address=mac_str,
            assigned_object_type=iface_ct,
            assigned_object_id=iface.pk,
        )
        iface.primary_mac_address = mac_obj
        iface.save()
        mac_count += 1

    if mac_count:
        log(f"  Set MAC address on {mac_count} interfaces")

    result = {"created": created, "updated": updated, "skipped": skipped, "breakout": breakout_count, "macs": mac_count}
    log(f"  Interfaces: {result}")
    return result


def sync_interface_templates(device_type, interfaces_data, log_fn=None):
    """Create InterfaceTemplates on a DeviceType if none exist.

    interfaces_data: dict from NAPALM get_interfaces().
    """
    log = log_fn or logger.info

    if device_type.interfacetemplates.exists():
        log(f"  DeviceType {device_type} already has interface templates, skipping")
        return {"created": 0}

    created = 0
    for name, data in interfaces_data.items():
        itype, mgmt_only = _classify_interface(name, data.get("speed", 0))
        InterfaceTemplate.objects.create(
            device_type=device_type,
            name=name,
            type=itype,
            mgmt_only=mgmt_only,
        )
        created += 1

    log(f"  Created {created} interface templates on {device_type}")
    return {"created": created}


def sync_ip_addresses(device, driver, log_fn=None):
    """Discover IP addresses from NAPALM and assign to interfaces.

    Returns dict with counts: {created, existing}.
    """
    log = log_fn or logger.info
    napalm_ips = driver.get_interfaces_ip()
    iface_ct = ContentType.objects.get_for_model(Interface)
    existing_ifaces = {
        iface.name: iface for iface in Interface.objects.filter(device=device)
    }

    created = existing = 0
    for iface_name, families in napalm_ips.items():
        iface = existing_ifaces.get(iface_name)
        if not iface:
            continue

        for family in ("ipv4", "ipv6"):
            for addr, info in families.get(family, {}).items():
                prefix_len = info.get("prefix_length", 32 if family == "ipv4" else 128)
                address_str = f"{addr}/{prefix_len}"

                # Check if this exact IP already exists on this interface
                ip_obj = IPAddress.objects.filter(
                    address=address_str,
                    assigned_object_type=iface_ct,
                    assigned_object_id=iface.pk,
                ).first()

                if ip_obj:
                    # Ensure VRF matches the interface's VRF
                    if iface.vrf_id and ip_obj.vrf_id != iface.vrf_id:
                        ip_obj.vrf = iface.vrf
                        ip_obj.save()
                        log(f"  Set VRF {iface.vrf} on {address_str}")
                    existing += 1
                    continue

                # Check if IP exists but is unassigned or on a different interface
                ip_obj = IPAddress.objects.filter(address=address_str).first()
                if ip_obj and ip_obj.assigned_object is None:
                    ip_obj.assigned_object = iface
                    ip_obj.vrf = iface.vrf
                    ip_obj.save()
                    created += 1
                    log(f"  Assigned {address_str} to {iface_name}")
                elif not ip_obj:
                    IPAddress.objects.create(
                        address=address_str,
                        assigned_object_type=iface_ct,
                        assigned_object_id=iface.pk,
                        vrf=iface.vrf,
                        status="active",
                    )
                    created += 1
                    log(f"  Created IP {address_str} on {iface_name}")
                else:
                    existing += 1

    result = {"created": created, "existing": existing}
    log(f"  IP addresses: {result}")
    return result


def _classify_prefix_role(iface_name):
    """Return a role name based on interface type."""
    lower = iface_name.lower()
    if lower.startswith("management") or lower == "eth0":
        return "Management"
    if lower.startswith("loopback"):
        return "Loopback"
    if lower.startswith("vlan"):
        return "Customer"
    return "Infrastructure"


def sync_prefixes(device, driver, log_fn=None):
    """Discover prefixes from interface IPs and create in NetBox.

    Derives the network prefix from each IP address and creates Prefix
    objects with appropriate VRF, VLAN, and Role associations.
    """
    log = log_fn or logger.info
    napalm_ips = driver.get_interfaces_ip()
    existing_ifaces = {
        iface.name: iface for iface in Interface.objects.filter(device=device)
    }

    # Get VLAN objects for VLAN interface -> Prefix association
    vlan_map = {}
    for vlan in VLAN.objects.filter(site=device.site):
        vlan_map[f"Vlan{vlan.vid}"] = vlan

    created = existing = 0
    for iface_name, families in napalm_ips.items():
        iface = existing_ifaces.get(iface_name)
        if not iface:
            continue

        for family in ("ipv4", "ipv6"):
            for addr, info in families.get(family, {}).items():
                prefix_len = info.get("prefix_length", 32 if family == "ipv4" else 128)

                # Skip host routes (/32 IPv4, /128 IPv6) — not real prefixes
                if (family == "ipv4" and prefix_len == 32) or (
                    family == "ipv6" and prefix_len == 128
                ):
                    # Exception: loopback /32s are still useful as prefixes
                    if not iface_name.lower().startswith("loopback"):
                        continue

                # Derive network prefix from address
                network = ipaddress.ip_network(
                    f"{addr}/{prefix_len}", strict=False
                )
                prefix_str = str(network)

                # Get or create the role
                role_name = _classify_prefix_role(iface_name)
                role, _ = Role.objects.get_or_create(
                    name=role_name,
                    defaults={"slug": role_name.lower()},
                )

                # Determine VRF and VLAN for this prefix
                vrf = iface.vrf
                vlan = vlan_map.get(iface_name)

                # Check if prefix already exists in this VRF
                pfx_exists = Prefix.objects.filter(
                    prefix=prefix_str,
                    vrf=vrf,
                ).exists()

                if pfx_exists:
                    existing += 1
                    continue

                Prefix.objects.create(
                    prefix=prefix_str,
                    vrf=vrf,
                    vlan=vlan,
                    scope=device.site,
                    role=role,
                    status=PrefixStatusChoices.STATUS_ACTIVE,
                )
                created += 1
                vrf_name = vrf.name if vrf else "global"
                log(f"  Created prefix {prefix_str} in VRF {vrf_name} (role: {role_name})")

    result = {"created": created, "existing": existing}
    log(f"  Prefixes: {result}")
    return result


def sync_lldp_cables(device, driver, log_fn=None):
    """Discover LLDP neighbors and create cables in NetBox.

    Only creates cables when both endpoints (devices + interfaces) exist
    in NetBox and no cable already exists.

    Returns dict with counts: {created, existing, skipped}.
    """
    log = log_fn or logger.info
    lldp = driver.get_lldp_neighbors_detail()
    local_ifaces = {
        iface.name: iface for iface in Interface.objects.filter(device=device)
    }

    created = existing = skipped = 0
    for local_name, neighbors in lldp.items():
        local_iface = local_ifaces.get(local_name)
        if not local_iface:
            skipped += 1
            continue

        # Already cabled?
        if local_iface.cable:
            existing += 1
            continue

        if len(neighbors) > 1:
            log(
                f"  WARNING: {local_name} has {len(neighbors)} LLDP neighbors "
                f"(unmanaged switch?) — cabling to first match only"
            )

        for nbr in neighbors:
            remote_hostname = nbr.get("remote_system_name", "")
            remote_port = nbr.get("remote_port", "")
            if not remote_hostname or not remote_port:
                skipped += 1
                continue

            # Find remote device — try exact match, then case-insensitive
            remote_device = (
                Device.objects.filter(name=remote_hostname).first()
                or Device.objects.filter(name__iexact=remote_hostname).first()
            )
            if not remote_device:
                skipped += 1
                continue

            # Find remote interface
            remote_iface = Interface.objects.filter(
                device=remote_device, name=remote_port
            ).first()
            if not remote_iface:
                skipped += 1
                continue

            # Already cabled from the other side?
            if remote_iface.cable:
                existing += 1
                continue

            # Create cable
            cable = Cable(
                status="connected",
                a_terminations=[local_iface],
                b_terminations=[remote_iface],
            )
            cable.save()
            created += 1
            log(f"  Cable: {device.name}:{local_name} <-> {remote_hostname}:{remote_port}")
            break  # One cable per interface — skip remaining neighbors

    result = {"created": created, "existing": existing, "skipped": skipped}
    log(f"  LLDP cables: {result}")
    return result


def _sync_evpn_rt_rd(device, driver, log_fn=None):
    """Fetch EVPN VNI state and L3VNI-to-VRF bindings.

    Returns dict with:
      _default_rd: router-id based RD for default VRF
      _l3vni_map: {vrf_name: {vni, local_as}} from sonic-vrf + BGP
      <vni_number>: {rd, import_rts, export_rts, type} for L2VNIs
    """
    log = log_fn or logger.info
    bgp_global_path = (
        "openconfig-network-instance:network-instances"
        "/network-instance=default/protocols/protocol=BGP,bgp/bgp/global"
    )
    bgp_global = driver._get_json(bgp_global_path)
    default_router_id = (
        bgp_global.get("openconfig-network-instance:global", {})
        .get("config", {})
        .get("router-id", "")
    )
    default_as = int(
        bgp_global.get("openconfig-network-instance:global", {})
        .get("config", {})
        .get("as", 0)
    )

    result = {"_default_rd": f"{default_router_id}:0" if default_router_id else ""}

    # Walk EVPN AFs for L2VNI RT/RD data
    for af in (
        bgp_global.get("openconfig-network-instance:global", {})
        .get("afi-safis", {})
        .get("afi-safi", [])
    ):
        evpn = af.get("l2vpn-evpn", {})
        vnis = evpn.get("openconfig-bgp-evpn-ext:vnis", {}).get("vni", [])
        for vni in vnis:
            state = vni.get("state", {})
            vni_num = state.get("vni-number", 0)
            rd = state.get("route-distinguisher", "")
            import_rts = state.get("import-rts", [])
            export_rts = state.get("export-rts", [])
            vni_type = state.get("type", "")

            result[vni_num] = {
                "rd": rd,
                "import_rts": import_rts,
                "export_rts": export_rts,
                "type": vni_type,
            }

    # Fetch L3VNI-to-VRF bindings from sonic-vrf
    l3vni_map = {}
    sonic_vrf = driver._get_json("sonic-vrf:sonic-vrf")
    for vrf_entry in (
        sonic_vrf.get("sonic-vrf:sonic-vrf", {})
        .get("VRF", {})
        .get("VRF_LIST", [])
    ):
        vrf_name = vrf_entry.get("vrf_name", "")
        vni = vrf_entry.get("vni")
        if vni and vrf_name != "default":
            # Get per-VRF BGP config for router-id and AS
            vrf_bgp_path = (
                "openconfig-network-instance:network-instances"
                f"/network-instance={vrf_name}/protocols/protocol=BGP,bgp/bgp/global/config"
            )
            vrf_bgp = driver._get_json(vrf_bgp_path)
            vrf_cfg = vrf_bgp.get("openconfig-network-instance:config", {})
            vrf_router_id = vrf_cfg.get("router-id", default_router_id)
            vrf_as = int(vrf_cfg.get("as", default_as))

            l3vni_map[vrf_name] = {
                "vni": vni,
                "local_as": vrf_as,
                "router_id": vrf_router_id,
            }

    result["_l3vni_map"] = l3vni_map
    return result


def sync_vrfs(device, driver, log_fn=None):
    """Discover VRFs, RDs, route targets, and assign interfaces.

    Uses get_network_instances() for VRF list, RESTCONF interface
    membership, and BGP EVPN VNI state for RD/RT data.
    """
    log = log_fn or logger.info
    instances = driver.get_network_instances()
    device_ifaces = {
        iface.name: iface for iface in Interface.objects.filter(device=device)
    }

    # Get EVPN RT/RD data
    evpn_data = _sync_evpn_rt_rd(device, driver, log)
    default_rd = evpn_data.pop("_default_rd", "")

    created = assigned = rt_count = 0
    for ni_name, ni_data in instances.items():
        ni_type = ni_data.get("type", "")
        # Skip L2VSIs
        if ni_type == "L2VSI":
            continue
        # Process both L3VRFs and DEFAULT_INSTANCE
        if ni_type not in ("L3VRF", "DEFAULT_INSTANCE"):
            continue

        # Get description from network-instance config
        ni_config_path = (
            "openconfig-network-instance:network-instances"
            f"/network-instance={ni_name}/config"
        )
        ni_config = driver._get_json(ni_config_path)
        oc_cfg = ni_config.get("openconfig-network-instance:config", {})
        description = oc_cfg.get("description", "")

        defaults = {}
        if description:
            defaults["description"] = description

        vrf, vrf_created = VRF.objects.get_or_create(
            name=ni_name,
            defaults=defaults,
        )
        if vrf_created:
            created += 1
            log(f"  Created VRF {ni_name}")

        # Get interface membership from RESTCONF
        ni_ifaces_path = (
            "openconfig-network-instance:network-instances"
            f"/network-instance={ni_name}/interfaces"
        )
        ni_ifaces_data = driver._get_json(ni_ifaces_path)
        member_ifaces = [
            i.get("id", "")
            for i in ni_ifaces_data.get(
                "openconfig-network-instance:interfaces", {}
            ).get("interface", [])
        ]

        # SONiC mgmt VRF: Management0 is implicitly a member
        if ni_name == "mgmt" and "Management0" not in member_ifaces:
            member_ifaces.append("Management0")

        for iface_name in member_ifaces:
            iface = device_ifaces.get(iface_name)
            if iface and iface.vrf_id != vrf.pk:
                iface.vrf = vrf
                iface.save()
                assigned += 1
                log(f"  Assigned {iface_name} to VRF {ni_name}")

    # Set RD on default VRF from router-id
    default_vrf = VRF.objects.filter(name="default").first()
    if default_vrf and not default_vrf.rd and default_rd:
        default_vrf.rd = default_rd
        default_vrf.save()
        log(f"  Set RD {default_rd} on default VRF")

    # Sync EVPN L2VNIs as L2VPN objects with RT/RD
    iface_ct = ContentType.objects.get_for_model(Interface)
    for vni_num, vni_data in evpn_data.items():
        # Skip metadata keys
        if isinstance(vni_num, str):
            continue
        rd = vni_data.get("rd", "")
        import_rts = vni_data.get("import_rts", [])
        export_rts = vni_data.get("export_rts", [])

        l2vpn_name = f"VNI-{vni_num}"
        l2vpn, l2vpn_created = L2VPN.objects.get_or_create(
            name=l2vpn_name,
            defaults={
                "slug": f"vni-{vni_num}",
                "type": L2VPNTypeChoices.TYPE_VXLAN_EVPN,
                "identifier": vni_num,
            },
        )
        if l2vpn_created:
            created += 1
            log(f"  Created L2VPN {l2vpn_name} (VXLAN-EVPN, VNI {vni_num})")

        # Add import/export RTs to the L2VPN
        for rt_name in import_rts:
            rt_obj, _ = RouteTarget.objects.get_or_create(name=rt_name)
            if rt_obj not in l2vpn.import_targets.all():
                l2vpn.import_targets.add(rt_obj)
                rt_count += 1
                log(f"  Added import RT {rt_name} to {l2vpn_name}")

        for rt_name in export_rts:
            rt_obj, _ = RouteTarget.objects.get_or_create(name=rt_name)
            if rt_obj not in l2vpn.export_targets.all():
                l2vpn.export_targets.add(rt_obj)
                rt_count += 1
                log(f"  Added export RT {rt_name} to {l2vpn_name}")

        # Find the VLAN mapped to this VNI via VXLAN tunnel map
        vlan_ct = ContentType.objects.get_for_model(VLAN)
        vxlan_maps = driver._get_json("sonic-vxlan:sonic-vxlan/VXLAN_TUNNEL_MAP")
        for tm in (
            vxlan_maps.get("sonic-vxlan:VXLAN_TUNNEL_MAP", {})
            .get("VXLAN_TUNNEL_MAP_LIST", [])
        ):
            if int(tm.get("vni", 0)) != vni_num:
                continue
            vlan_name = tm.get("vlan", "")  # e.g. "Vlan100"
            m = re.match(r"Vlan(\d+)", vlan_name)
            if not m:
                continue
            vlan_vid = int(m.group(1))

            # Skip L3VNI transit VLANs (not real L2 service VLANs)
            l3vni_map = evpn_data.get("_l3vni_map", {})
            is_l3vni_vlan = any(
                info["vni"] == vni_num for info in l3vni_map.values()
            )
            if is_l3vni_vlan:
                continue

            vlan_obj, vlan_created = VLAN.objects.get_or_create(
                vid=vlan_vid,
                site=device.site,
                defaults={
                    "name": vlan_name,
                    "status": VLANStatusChoices.STATUS_ACTIVE,
                },
            )
            if vlan_created:
                log(f"  Created VLAN {vlan_vid} ({vlan_name})")

            term_exists = L2VPNTermination.objects.filter(
                assigned_object_type=vlan_ct,
                assigned_object_id=vlan_obj.pk,
            ).exists()
            if not term_exists:
                L2VPNTermination.objects.create(
                    l2vpn=l2vpn,
                    assigned_object_type=vlan_ct,
                    assigned_object_id=vlan_obj.pk,
                )
                log(f"  Terminated {l2vpn_name} on VLAN {vlan_vid}")

    # Sync L3VNI bindings — set RD and RT on tenant VRFs
    l3vni_map = evpn_data.get("_l3vni_map", {})
    for vrf_name, l3vni_info in l3vni_map.items():
        vni = l3vni_info["vni"]
        local_as = l3vni_info["local_as"]
        router_id = l3vni_info["router_id"]

        vrf = VRF.objects.filter(name=vrf_name).first()
        if not vrf:
            continue

        # Auto-derive RD: router-id:VNI (SONiC's default behavior)
        derived_rd = f"{router_id}:{vni}"
        if not vrf.rd or vrf.rd != derived_rd:
            vrf.rd = derived_rd
            vrf.save()
            log(f"  Set RD {derived_rd} on VRF {vrf_name} (L3VNI {vni})")

        # Auto-derive RT: AS:VNI (SONiC's default for L3VNI)
        derived_rt = f"{local_as}:{vni}"
        rt_obj, _ = RouteTarget.objects.get_or_create(name=derived_rt)
        if rt_obj not in vrf.import_targets.all():
            vrf.import_targets.add(rt_obj)
            rt_count += 1
            log(f"  Added import RT {derived_rt} to VRF {vrf_name}")
        if rt_obj not in vrf.export_targets.all():
            vrf.export_targets.add(rt_obj)
            rt_count += 1
            log(f"  Added export RT {derived_rt} to VRF {vrf_name}")

    result = {"created": created, "assigned": assigned, "route_targets": rt_count}
    if created or assigned or rt_count:
        log(f"  VRFs: {result}")
    return result


def sync_global_macs(device, driver, log_fn=None):
    """Discover global/anycast MAC addresses (MC-LAG, SAG, base MAC).

    These are device-level MACs not tied to a specific interface.
    Created as MACAddress objects with description but no assigned_object.
    """
    log = log_fn or logger.info
    created = 0

    mac_sources = []

    # MC-LAG MACs
    mclag_data = driver._get_json("sonic-mclag:sonic-mclag")
    if mclag_data:
        mclag = mclag_data.get("sonic-mclag:sonic-mclag", {})
        # Gateway MAC
        for gw in mclag.get("MCLAG_GW_MAC", {}).get("MCLAG_GW_MAC_LIST", []):
            mac = gw.get("gw_mac", "")
            if mac:
                mac_sources.append((mac, "MC-LAG gateway MAC"))
        # System MAC
        for domain in mclag.get("MCLAG_DOMAIN", {}).get("MCLAG_DOMAIN_LIST", []):
            mac = domain.get("mclag_system_mac", "")
            if mac:
                mac_sources.append((mac, "MC-LAG system MAC"))

    # SAG (Static Anycast Gateway) MAC
    sag_data = driver._get_json("sonic-sag:sonic-sag")
    if sag_data:
        sag = sag_data.get("sonic-sag:sonic-sag", {})
        sag_global = sag.get("SAG_GLOBAL", {}).get("SAG_GLOBAL_LIST", [{}])
        for entry in sag_global if isinstance(sag_global, list) else [sag_global]:
            mac = entry.get("gwmac", "")
            if mac:
                mac_sources.append((mac, "SAG anycast gateway MAC"))

    # Base MAC from system EEPROM
    eeprom = driver._get_json(
        "openconfig-platform:components/component=System%20Eeprom/state"
    )
    base_mac = eeprom.get("openconfig-platform:state", {}).get(
        "openconfig-platform-ext:base-mac-address", ""
    )
    if base_mac and base_mac != "00:00:00:00:00:00":
        mac_sources.append((base_mac, "System base MAC (EEPROM)"))

    # Create MACAddress objects without interface assignment
    for mac_str, description in mac_sources:
        existing = MACAddress.objects.filter(
            mac_address=mac_str,
            assigned_object_type__isnull=True,
            assigned_object_id__isnull=True,
        ).first()
        if not existing:
            MACAddress.objects.create(
                mac_address=mac_str,
                description=description,
            )
            created += 1
            log(f"  Created global MAC {mac_str} ({description})")

    if created:
        log(f"  Global MACs: {created} created")
    return {"created": created}


def sync_device_facts(device, driver, log_fn=None):
    """Update device fields from NAPALM get_facts() and sync ASNs from BGP."""
    log = log_fn or logger.info
    facts = driver.get_facts()

    changed = []
    serial = facts.get("serial_number", "")
    if serial and device.serial != serial:
        device.serial = serial
        changed.append("serial")

    if changed:
        device.save()
        log(f"  Updated device fields: {', '.join(changed)}")

    # Sync ASNs from BGP neighbors (local_as)
    asns_created = 0
    try:
        bgp = driver.get_bgp_neighbors()
        for vrf_name, vrf_data in bgp.items():
            peers = vrf_data.get("peers", {})
            # Collect all unique AS numbers (local + remote)
            as_numbers = set()
            router_id = vrf_data.get("router_id", "")
            for peer_addr, peer_data in peers.items():
                local_as = peer_data.get("local_as", 0)
                remote_as = peer_data.get("remote_as", 0)
                if local_as:
                    as_numbers.add(local_as)
                if remote_as:
                    as_numbers.add(remote_as)

            # Get or create a default RIR for private ASNs
            rir, _ = RIR.objects.get_or_create(
                name="Private",
                defaults={"slug": "private", "is_private": True},
            )

            for asn_number in as_numbers:
                asn_obj, asn_created = ASN.objects.get_or_create(
                    asn=asn_number,
                    defaults={"rir": rir},
                )
                if asn_created:
                    asns_created += 1
                    log(f"  Created ASN {asn_number}")

                # Associate with the device's site
                if device.site and asn_obj not in device.site.asns.all():
                    device.site.asns.add(asn_obj)
                    log(f"  Associated AS{asn_number} with site {device.site}")
    except Exception as e:
        log(f"  ASN sync error: {e}")

    return {"updated_fields": changed, "asns_created": asns_created, "facts": facts}


def full_sync(device, log_fn=None):
    """Run all discovery steps for a device.

    Returns a summary dict.
    """
    log = log_fn or logger.info
    log(f"Starting sync for {device.name} ({device.primary_ip})...")

    driver = get_napalm_driver(device)
    try:
        results = {}

        # 1. Sync interfaces
        results["interfaces"] = sync_interfaces(device, driver, log)

        # 2. Optionally backfill DeviceType templates
        if get_plugin_config(
            "netbox_sonic_discovery", "create_missing_interface_templates"
        ):
            napalm_ifaces = driver.get_interfaces()
            results["templates"] = sync_interface_templates(
                device.device_type, napalm_ifaces, log
            )

        # 3. Sync IP addresses
        if get_plugin_config("netbox_sonic_discovery", "sync_ip_addresses"):
            results["ip_addresses"] = sync_ip_addresses(device, driver, log)

        # 4. Sync prefixes (derived from IPs)
        results["prefixes"] = sync_prefixes(device, driver, log)

        # 5. Sync LLDP cables
        if get_plugin_config("netbox_sonic_discovery", "sync_cables_from_lldp"):
            results["cables"] = sync_lldp_cables(device, driver, log)

        # 5. Sync VRFs
        results["vrfs"] = sync_vrfs(device, driver, log)

        # 6. Sync global MACs (MC-LAG, SAG, base MAC)
        results["global_macs"] = sync_global_macs(device, driver, log)

        # 7. Sync device facts
        results["facts"] = sync_device_facts(device, driver, log)

        log(f"Sync complete for {device.name}")
        return results

    finally:
        driver.close()
