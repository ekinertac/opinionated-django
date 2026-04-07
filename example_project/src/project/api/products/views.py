from rest_framework import status, viewsets
from rest_framework.response import Response

from products.dtos.product import ProductDTO
from products.services.product import ProductService
from project.services import get

from .serializers import CreateProductSerializer


class ProductViewSet(viewsets.ViewSet):
    def list(self, request):
        products = get(ProductService).list_products()
        return Response([dto.model_dump() for dto in products])

    def create(self, request):
        serializer = CreateProductSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        dto = get(ProductService).create_product(**serializer.validated_data)
        return Response(dto.model_dump(), status=status.HTTP_201_CREATED)

    def retrieve(self, request, pk=None):
        dto = get(ProductService).get_product(pk)
        return Response(dto.model_dump())
