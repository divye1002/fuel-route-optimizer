from rest_framework import serializers


class RouteRequestSerializer(serializers.Serializer):
    """start/finish accept a place string or a raw 'lat,lng'."""

    start = serializers.CharField(allow_blank=False, trim_whitespace=True)
    finish = serializers.CharField(allow_blank=False, trim_whitespace=True)
