"""Topology YAML loader."""

import yaml


def load_topology(path: str) -> dict:
    with open(path) as f:
        return yaml.safe_load(f)
