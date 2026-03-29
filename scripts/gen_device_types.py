#!/usr/bin/env python3
"""Generate NetBox device-type YAML files from SONiC port_config.ini dumps.

Reads the combined port_config dump (from ref/port_configs_*.txt) and produces
one YAML per SKU in the output directory, with interface names matching SONiC
standard naming mode (Eth1/1, Eth1/2, ...).

Usage:
    python3 scripts/gen_device_types.py ref/port_configs_442.txt device-types/
"""

import os
import re
import sys
from collections import OrderedDict


# Map speed (Mbps) to NetBox interface type slug
SPEED_TO_TYPE = {
    1000: "1000base-t",
    2500: "2500base-t",
    10000: "10gbase-x-sfpp",
    25000: "25gbase-x-sfp28",
    40000: "40gbase-x-qsfpp",
    50000: "50gbase-x-sfp28",
    100000: "100gbase-x-qsfp28",
    200000: "200gbase-x-qsfp56",
    400000: "400gbase-x-osfp",
}

# Alias prefix hints for interface type when speed alone is ambiguous
ALIAS_TYPE_HINTS = {
    "oneGigE": "1000base-t",
    "twentyfiveGigE": "25gbase-x-sfp28",
    "tenGigE": "10gbase-x-sfpp",
    "fortyGigE": "40gbase-x-qsfpp",
    "fiftyGigE": "50gbase-x-sfp28",
    "hundredGigE": "100gbase-x-qsfp28",
    "twoHundredGigE": "200gbase-x-qsfp56",
    "fourHundredGigE": "400gbase-x-osfp",
    "TwentyFiveGigE": "25gbase-x-sfp28",
    "TenGigabitEthernet": "10gbase-x-sfpp",
    "HundredGigE": "100gbase-x-qsfp28",
    "FourHundredGigE": "400gbase-x-osfp",
}


def alias_to_sonic_name(alias: str, index: int) -> str:
    """Convert OS10-style alias to SONiC standard naming.

    Examples:
        twentyfiveGigE1/1/1  -> Eth1/1
        hundredGigE1/49      -> Eth1/49
        oneGigE1/1           -> Eth1/1
        TenGigabitEthernet 1/1 -> Eth1/1
    """
    # Strip the speed prefix to get the port numbering
    # Patterns: prefixX/Y/Z, prefixX/Y, prefix X/Y
    m = re.match(r'[a-zA-Z]+\s*(\d+/.+)', alias)
    if not m:
        return f"Eth1/{index}"

    port_part = m.group(1)
    # Normalize: 1/1/1 -> use index, 1/49 -> use as-is
    segments = port_part.split("/")
    if len(segments) == 3:
        # twentyfiveGigE1/1/1 -> Eth1/{index}
        return f"Eth1/{index}"
    elif len(segments) == 2:
        # hundredGigE1/49 -> Eth1/49
        return f"Eth{segments[0]}/{segments[1]}"
    else:
        return f"Eth1/{index}"


def iface_type_from_alias_and_speed(alias: str, speed: int) -> str:
    """Determine NetBox interface type from alias prefix and speed."""
    for prefix, ntype in ALIAS_TYPE_HINTS.items():
        if alias.startswith(prefix):
            return ntype
    return SPEED_TO_TYPE.get(speed, "other")


def parse_port_configs(filepath: str) -> dict:
    """Parse combined port_config dump into {sku: [(name, alias, index, speed), ...]}."""
    skus = OrderedDict()
    current_sku = None

    with open(filepath) as f:
        for line in f:
            line = line.rstrip()
            if line.startswith("### SKU: "):
                current_sku = line.split("### SKU: ", 1)[1].strip()
                skus[current_sku] = []
                continue
            if not current_sku or line.startswith("#") or not line.strip():
                continue

            parts = line.split()
            if len(parts) < 5:
                continue

            internal_name = parts[0]  # e.g. Ethernet0
            # lanes = parts[1]  (not needed)
            alias = parts[2]  # e.g. twentyfiveGigE1/1/1
            index = int(parts[3])
            speed = int(parts[4])

            skus[current_sku].append((internal_name, alias, index, speed))

    return skus


def sku_to_slug(sku: str) -> str:
    """Convert SKU name to a URL-friendly slug."""
    return sku.lower().replace(" ", "-")


def sku_to_model(sku: str) -> str:
    """Convert SKU to a human-readable model name."""
    # DellEMC-S5248f-P-25G-DPB -> S5248F-ON (P-25G-DPB)
    # Just return the SKU as the model for now
    return sku


def generate_yaml(sku: str, ports: list) -> str:
    """Generate a NetBox device-type YAML for one SKU."""
    lines = [
        "---",
        "manufacturer: Dell",
        f"model: '{sku}'",
        f"slug: '{sku_to_slug(sku)}'",
        "u_height: 1",
        "is_full_depth: true",
        "console-ports:",
        "  - name: Console",
        "    type: rj-45",
        "power-ports:",
        "  - name: PS1",
        "    type: iec-60320-c14",
        "  - name: PS2",
        "    type: iec-60320-c14",
        "interfaces:",
    ]

    for internal_name, alias, index, speed in ports:
        sonic_name = alias_to_sonic_name(alias, index)
        itype = iface_type_from_alias_and_speed(alias, speed)
        lines.append(f"  - name: '{sonic_name}'")
        lines.append(f"    type: {itype}")

    # Add management interface
    lines.append("  - name: Management0")
    lines.append("    type: 1000base-t")
    lines.append("    mgmt_only: true")

    return "\n".join(lines) + "\n"


def main():
    if len(sys.argv) < 3:
        print(f"Usage: {sys.argv[0]} <port_configs.txt> <output_dir>")
        return 1

    input_file = sys.argv[1]
    output_dir = sys.argv[2]
    os.makedirs(output_dir, exist_ok=True)

    skus = parse_port_configs(input_file)
    print(f"Parsed {len(skus)} SKUs from {input_file}")

    for sku, ports in skus.items():
        if not ports:
            continue
        yaml_content = generate_yaml(sku, ports)
        filename = f"{sku}.yaml"
        filepath = os.path.join(output_dir, filename)
        with open(filepath, "w") as f:
            f.write(yaml_content)
        print(f"  {filename}: {len(ports)} ports")

    print(f"\nGenerated {len(skus)} device-type files in {output_dir}/")
    return 0


if __name__ == "__main__":
    sys.exit(main())
