from rest_framework import serializers


class OrderItemSerializer(serializers.Serializer):
    product_id = serializers.CharField()
    quantity = serializers.IntegerField()


class CreateOrderSerializer(serializers.Serializer):
    items = OrderItemSerializer(many=True)
