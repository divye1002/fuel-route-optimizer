from django.db import models


class FuelStation(models.Model):
    name = models.CharField(max_length=255)
    address = models.CharField(max_length=512, blank=True)
    city = models.CharField(max_length=128, blank=True)
    state = models.CharField(max_length=8, blank=True)
    price_per_gallon = models.FloatField()       # retail USD/gal
    latitude = models.FloatField(null=True)
    longitude = models.FloatField(null=True)

    class Meta:
        indexes = [models.Index(fields=["latitude", "longitude"])]

    def __str__(self):
        return f"{self.name} ({self.city}, {self.state}) ${self.price_per_gallon:.2f}"
