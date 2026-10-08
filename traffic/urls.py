from django.urls import path

from traffic.views import (
    ControllerEventAPIView,
    JunctionCommandAPIView,
    JunctionDetailAPIView,
    JunctionHistoryAPIView,
    JunctionListAPIView,
    JunctionStatusAPIView,
    SensorEventAPIView,
)


urlpatterns = [
    path(
        "junctions/",
        JunctionListAPIView.as_view(),
        name="junction-list",
    ),

    path(
        "junctions/<str:junction_id>/",
        JunctionDetailAPIView.as_view(),
        name="junction-detail",
    ),

    path(
        "junctions/<str:junction_id>/status/",
        JunctionStatusAPIView.as_view(),
        name="junction-status",
    ),

    path(
        "junctions/<str:junction_id>/history/",
        JunctionHistoryAPIView.as_view(),
        name="junction-history",
    ),

    path(
        "junctions/<str:junction_id>/commands/",
        JunctionCommandAPIView.as_view(),
        name="junction-command",
    ),

    path(
        "sensor-events/",
        SensorEventAPIView.as_view(),
        name="sensor-event",
    ),

    path(
        "controller-events/",
        ControllerEventAPIView.as_view(),
        name="controller-event",
    ),
]