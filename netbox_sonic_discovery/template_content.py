"""Inject a Sync button on the device detail page."""

from netbox.plugins import PluginTemplateExtension


class SonicDiscoveryDeviceButtons(PluginTemplateExtension):
    models = ["dcim.device"]

    def buttons(self):
        device = self.context["object"]
        if not (
            device.platform
            and hasattr(device.platform, "napalm")
            and device.platform.napalm.napalm_driver
            and device.status == "active"
            and device.primary_ip
        ):
            return ""
        return self.render(
            "netbox_sonic_discovery/device_sync_button.html",
            extra_context={"device": device},
        )


template_extensions = [SonicDiscoveryDeviceButtons]
