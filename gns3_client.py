"""Reusable GNS3 REST API client."""

import time

import requests


class GNS3Client:
    """Thin wrapper around the GNS3 v2 REST API."""

    def __init__(self, base_url="http://192.168.70.200:3080/v2"):
        self.base_url = base_url.rstrip("/")
        self.session = requests.Session()
        self.session.headers["Content-Type"] = "application/json"

    def _url(self, path: str) -> str:
        return f"{self.base_url}/{path.lstrip('/')}"

    def _get(self, path: str) -> dict | list:
        r = self.session.get(self._url(path))
        r.raise_for_status()
        return r.json()

    def _post(self, path: str, json: dict | None = None) -> dict:
        r = self.session.post(self._url(path), json=json or {})
        r.raise_for_status()
        if r.text:
            return r.json()
        return {}

    def _put(self, path: str, json: dict | None = None) -> dict:
        r = self.session.put(self._url(path), json=json or {})
        r.raise_for_status()
        return r.json()

    def _delete(self, path: str) -> None:
        r = self.session.delete(self._url(path))
        r.raise_for_status()

    # -- Projects -----------------------------------------------------------

    def create_project(self, name: str) -> dict:
        return self._post("projects", {"name": name})

    def delete_project(self, project_id: str) -> None:
        self._delete(f"projects/{project_id}")

    def open_project(self, project_id: str) -> dict:
        return self._post(f"projects/{project_id}/open")

    def close_project(self, project_id: str) -> None:
        self._post(f"projects/{project_id}/close")

    def get_project(self, project_id: str) -> dict:
        return self._get(f"projects/{project_id}")

    def list_projects(self) -> list:
        return self._get("projects")

    def find_project(self, name: str) -> dict | None:
        """Find most recent project matching name (prefix match)."""
        projects = self.list_projects()
        matches = [p for p in projects if p["name"].startswith(name)]
        if not matches:
            return None
        # Return most recently created (by name suffix = timestamp)
        return sorted(matches, key=lambda p: p["name"])[-1]

    # -- Nodes --------------------------------------------------------------

    def create_node_from_template(self, project_id: str, template_id: str,
                                  name: str, x: int = 0, y: int = 0) -> dict:
        return self._post(f"projects/{project_id}/templates/{template_id}", {
            "name": name,
            "x": x,
            "y": y,
        })

    def create_node(self, project_id: str, node_type: str, name: str,
                    x: int = 0, y: int = 0,
                    compute_id: str = "local", **properties) -> dict:
        """Create a node directly (for built-in types like ethernet_switch, cloud)."""
        payload = {
            "name": name,
            "node_type": node_type,
            "compute_id": compute_id,
            "x": x,
            "y": y,
        }
        if properties:
            payload["properties"] = properties
        return self._post(f"projects/{project_id}/nodes", payload)

    def get_node(self, project_id: str, node_id: str) -> dict:
        return self._get(f"projects/{project_id}/nodes/{node_id}")

    def update_node(self, project_id: str, node_id: str, **kwargs) -> dict:
        """Update node properties (e.g. name for label renaming)."""
        return self._put(f"projects/{project_id}/nodes/{node_id}", kwargs)

    def get_nodes(self, project_id: str) -> list:
        return self._get(f"projects/{project_id}/nodes")

    def start_node(self, project_id: str, node_id: str) -> None:
        self._post(f"projects/{project_id}/nodes/{node_id}/start")

    def stop_node(self, project_id: str, node_id: str) -> None:
        self._post(f"projects/{project_id}/nodes/{node_id}/stop")

    def start_all_nodes(self, project_id: str) -> None:
        self._post(f"projects/{project_id}/nodes/start")

    def stop_all_nodes(self, project_id: str) -> None:
        self._post(f"projects/{project_id}/nodes/stop")

    # -- Links --------------------------------------------------------------

    def create_link(self, project_id: str,
                    node_a_id: str, adapter_a: int, port_a: int,
                    node_b_id: str, adapter_b: int, port_b: int) -> dict:
        return self._post(f"projects/{project_id}/links", {
            "nodes": [
                {"node_id": node_a_id, "adapter_number": adapter_a, "port_number": port_a},
                {"node_id": node_b_id, "adapter_number": adapter_b, "port_number": port_b},
            ]
        })

    def get_links(self, project_id: str) -> list:
        return self._get(f"projects/{project_id}/links")

    def delete_link(self, project_id: str, link_id: str) -> None:
        self._delete(f"projects/{project_id}/links/{link_id}")
