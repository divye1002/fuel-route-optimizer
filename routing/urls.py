from django.urls import path

from routing.views import RouteAPIView, RouteMapView

urlpatterns = [
    path("route/", RouteAPIView.as_view(), name="route"),
    path("route/map/", RouteMapView.as_view(), name="route-map"),
]
