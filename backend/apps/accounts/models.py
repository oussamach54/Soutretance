from django.contrib.auth.models import AbstractUser
from django.db import models


class User(AbstractUser):
    """One private workspace per subcontractor account."""

    company_name = models.CharField(max_length=180, blank=True)
    phone = models.CharField(max_length=32, blank=True)
