from django.urls import path

from . import views

app_name = "netbox_device_importer"

urlpatterns = [
    path("", views.DeviceImportView.as_view(), name="import"),
    path("template/", views.TemplateDownloadView.as_view(), name="template"),
    path("report/", views.ReportDownloadView.as_view(), name="report"),
    path("status/", views.ImportStatusView.as_view(), name="status"),
    path("stop/", views.ImportStopView.as_view(), name="stop"),
]
