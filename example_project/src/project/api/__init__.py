from django.urls import include, path
from rest_framework.routers import DefaultRouter

from project.api.orders.views import OrderViewSet
from project.api.products.views import ProductViewSet

router = DefaultRouter()
router.register(r"products", ProductViewSet, basename="product")
router.register(r"orders", OrderViewSet, basename="order")

urlpatterns = [
    path("", include(router.urls)),
]
