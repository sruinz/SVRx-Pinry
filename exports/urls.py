from django.urls import path

from exports.views import ExportCreateView, ExportPreviewView


urlpatterns = [
    path("preview/", ExportPreviewView.as_view(), name="export-preview"),
    path("", ExportCreateView.as_view(), name="export-create"),
]
