"""Post-ready host configuration: hostnames, interface enable, IP assignment."""

from . import log
from .interfaces import topo_iface_to_guest
from .sonic_rest import sonic_patch
from .ssh import ssh_cmd


def set_hostname_sonic(ip: str, hostname: str, auth: tuple) -> bool:
    """Set hostname on SONiC via RESTCONF."""
    try:
        r = sonic_patch(ip, "data/openconfig-system:system/config",
                        {"openconfig-system:config": {"hostname": hostname}}, auth)
        if r.status_code in (200, 204):
            log(f"  {hostname}: hostname set via RESTCONF")
            return True
        log(f"  {hostname}: hostname PATCH returned {r.status_code}: {r.text}")
        return False
    except Exception as e:
        log(f"  {hostname}: hostname error: {e}")
        return False


def set_hostname_debian(ip: str, hostname: str, debian_auth: tuple) -> bool:
    """Set hostname on Debian via SSH and update lldpd."""
    rc, out = ssh_cmd(ip, f"sudo hostnamectl set-hostname {hostname}",
                      debian_auth[0], debian_auth[1])
    if rc != 0:
        log(f"  {hostname}: hostname error: {out}")
        return False
    # Update lldpd so it advertises the new hostname immediately
    ssh_cmd(ip, f"sudo /usr/sbin/lldpcli configure system hostname {hostname}",
            debian_auth[0], debian_auth[1])
    log(f"  {hostname}: hostname set via SSH")
    return True


def enable_interface_sonic(ip: str, native_iface: str, auth: tuple) -> bool:
    """Enable a SONiC interface via RESTCONF (no shutdown)."""
    try:
        r = sonic_patch(
            ip,
            f"data/openconfig-interfaces:interfaces/interface={native_iface}/config",
            {"openconfig-interfaces:config": {"enabled": True}}, auth)
        return r.status_code in (200, 204)
    except Exception as e:
        log(f"  {ip}: enable {native_iface} error: {e}")
        return False


def enable_interface_debian(ip: str, guest_iface: str, debian_auth: tuple) -> bool:
    """Bring up a Debian interface via SSH."""
    rc, _ = ssh_cmd(ip, f"sudo ip link set {guest_iface} up",
                    debian_auth[0], debian_auth[1])
    return rc == 0


def configure_host_ip(ip: str, topo_iface: str, host_ip: str,
                      debian_auth: tuple) -> bool:
    """Assign an IP address to a Debian host interface and bring it up."""
    guest_iface = topo_iface_to_guest(topo_iface)
    rc1, out1 = ssh_cmd(ip, f"sudo ip addr add {host_ip} dev {guest_iface}",
                        debian_auth[0], debian_auth[1])
    rc2, out2 = ssh_cmd(ip, f"sudo ip link set {guest_iface} up",
                        debian_auth[0], debian_auth[1])
    if rc1 != 0:
        log(f"  {ip}: ip addr add failed: {out1}")
        return False
    if rc2 != 0:
        log(f"  {ip}: link set up failed: {out2}")
        return False
    return True
