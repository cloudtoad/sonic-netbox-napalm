from django.urls import path

from . import views

urlpatterns = [
    path(
        "devices/<int:pk>/sync/",
        views.DeviceSonicSyncActionView.as_view(),
        name="device_sonic_sync",
    ),
]
