from django.contrib import admin
from django.urls import include, path

admin.site.site_header = "Mailadmin Finestar"
admin.site.site_title = "Mailadmin"
admin.site.index_title = "Mail Operations"

urlpatterns = [
    path("admin/", admin.site.urls),
    path("", include("mailops.urls")),
]
