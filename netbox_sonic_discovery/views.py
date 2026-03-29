"""Views for the SONiC discovery plugin — device detail tab + sync action."""

from dcim.models import Device
from django.contrib import messages
from django.shortcuts import get_object_or_404, redirect
from django.utils.translation import gettext as _
from django.views import View
from netbox.views import generic
from utilities.views import ViewTab, register_model_view

from .discovery import full_sync


class SonicDiscoveryTab(ViewTab):
    """Tab that only appears on SONiC devices with NAPALM configured."""

    def render(self, instance):
        if not (
            instance.platform
            and hasattr(instance.platform, "napalm")
            and instance.platform.napalm.napalm_driver
            and instance.status == "active"
            and instance.primary_ip
        ):
            return None
        return super().render(instance)


@register_model_view(Device, "sonic_sync", path="sonic-sync")
class DeviceSonicSyncView(generic.ObjectView):
    queryset = Device.objects.all()
    template_name = "netbox_sonic_discovery/device_sync_tab.html"
    tab = SonicDiscoveryTab(
        label=_("SONiC Sync"),
        weight=3300,
    )


class DeviceSonicSyncActionView(View):
    """POST handler to trigger a full sync for a device."""

    def post(self, request, pk):
        device = get_object_or_404(Device, pk=pk)
        log_messages = []

        def log_fn(msg):
            log_messages.append(msg)

        try:
            results = full_sync(device, log_fn=log_fn)
            summary_parts = []
            if results.get("interfaces", {}).get("created"):
                summary_parts.append(
                    f"{results['interfaces']['created']} interfaces created"
                )
            if results.get("ip_addresses", {}).get("created"):
                summary_parts.append(
                    f"{results['ip_addresses']['created']} IPs created"
                )
            if results.get("cables", {}).get("created"):
                summary_parts.append(
                    f"{results['cables']['created']} cables created"
                )
            summary = ", ".join(summary_parts) if summary_parts else "Already in sync"
            messages.success(request, f"SONiC sync complete: {summary}")
        except Exception as e:
            messages.error(request, f"SONiC sync failed: {e}")

        return redirect("dcim:device", pk=pk)
