from rest_framework import status, viewsets
from rest_framework.response import Response

from orders.dtos.order import OrderDTO
from orders.services.order import OrderService
from project.services import get

from .serializers import CreateOrderSerializer


class OrderViewSet(viewsets.ViewSet):
    def list(self, request):
        orders = get(OrderService).list_orders()
        return Response([dto.model_dump() for dto in orders])

    def create(self, request):
        serializer = CreateOrderSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        items = serializer.validated_data["items"]
        dto = get(OrderService).create_order(items=items)
        return Response(dto.model_dump(), status=status.HTTP_201_CREATED)

    def retrieve(self, request, pk=None):
        dto = get(OrderService).get_order(pk)
        return Response(dto.model_dump())
