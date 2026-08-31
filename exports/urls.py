from django.urls import path

from exports.views import (
    ExportCreateView,
    ExportDownloadView,
    ExportLatestView,
    ExportPreviewView,
    ExportStatusView,
)


urlpatterns = [
    path("preview/", ExportPreviewView.as_view(), name="export-preview"),
    path("latest/", ExportLatestView.as_view(), name="export-latest"),
    path(
        "<uuid:job_uuid>/download/",
        ExportDownloadView.as_view(),
        name="export-download",
    ),
    path("<uuid:job_uuid>/", ExportStatusView.as_view(), name="export-status"),
    path("", ExportCreateView.as_view(), name="export-create"),
]
