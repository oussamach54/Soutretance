from decimal import Decimal

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import MinValueValidator
from django.db import models
from django.db.models import Q

from apps.workshop.models import Article, Seamstress


class SeamstressRate(models.Model):
    owner = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    article = models.ForeignKey(Article, related_name="custom_rates", on_delete=models.PROTECT)
    seamstress = models.ForeignKey(Seamstress, related_name="custom_rates", on_delete=models.PROTECT)
    unit_rate = models.DecimalField(
        max_digits=10, decimal_places=2,
        validators=[MinValueValidator(Decimal("0.00"))],
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["owner", "article", "seamstress"], name="uniq_seamstress_article_rate"),
            models.CheckConstraint(
                condition=Q(unit_rate__gte=Decimal("0.00")),
                name="chk_seamstress_rate_not_negative",
            ),
        ]

    def clean(self):
        super().clean()
        if self.owner_id:
            if self.article_id and self.article.owner_id != self.owner_id:
                raise ValidationError({"article": "L'article doit appartenir au même sous-traitant."})
            if self.seamstress_id and self.seamstress.owner_id != self.owner_id:
                raise ValidationError({"seamstress": "La couturière doit appartenir au même sous-traitant."})

    def save(self, *args, **kwargs):
        self.full_clean()
        return super().save(*args, **kwargs)


class SeamstressRateHistory(models.Model):
    """Immutable history of a seamstress/article rate change."""

    owner = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    article = models.ForeignKey(Article, related_name="rate_history", on_delete=models.PROTECT)
    seamstress = models.ForeignKey(Seamstress, related_name="rate_history", on_delete=models.PROTECT)
    unit_rate = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        validators=[MinValueValidator(Decimal("0.00"))],
    )
    effective_on = models.DateField()
    source = models.CharField(max_length=80, default="manual")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=Q(unit_rate__gte=Decimal("0.00")),
                name="chk_seamstress_rate_history_not_negative",
            ),
        ]
        indexes = [models.Index(fields=["owner", "seamstress", "article", "effective_on"])]

    def clean(self):
        super().clean()
        if self.owner_id:
            if self.article_id and self.article.owner_id != self.owner_id:
                raise ValidationError({"article": "L'article doit appartenir au même sous-traitant."})
            if self.seamstress_id and self.seamstress.owner_id != self.owner_id:
                raise ValidationError({"seamstress": "La couturière doit appartenir au même sous-traitant."})

    def save(self, *args, **kwargs):
        if self.pk:
            raise ValidationError("Un historique de tarif ne peut pas être modifié.")
        self.full_clean()
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError("Un historique de tarif ne peut pas être supprimé.")


class Payment(models.Model):
    class Kind(models.TextChoices):
        ADVANCE = "advance", "Avance"
        SETTLEMENT = "settlement", "Règlement"

    class Status(models.TextChoices):
        RECORDED = "recorded", "Enregistré"
        CANCELLED = "cancelled", "Annulé"
        CORRECTED = "corrected", "Corrigé"

    owner = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    seamstress = models.ForeignKey(Seamstress, related_name="payments", on_delete=models.PROTECT)
    amount = models.DecimalField(max_digits=12, decimal_places=2, validators=[MinValueValidator(Decimal("0.01"))])
    paid_on = models.DateField()
    kind = models.CharField(max_length=16, choices=Kind.choices, default=Kind.SETTLEMENT)
    method = models.CharField(max_length=40, blank=True)
    comments = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.RECORDED)
    correction_of = models.ForeignKey(
        "self",
        null=True,
        blank=True,
        related_name="corrections",
        on_delete=models.PROTECT,
    )
    cancelled_at = models.DateTimeField(null=True, blank=True)
    cancelled_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        related_name="+",
        on_delete=models.PROTECT,
    )
    cancellation_reason = models.TextField(blank=True)

    class Meta:
        indexes = [models.Index(fields=["owner", "seamstress", "paid_on"])]
        constraints = [
            models.CheckConstraint(
                condition=Q(amount__gte=Decimal("0.01")),
                name="chk_payment_amount_positive",
            ),
        ]

    def clean(self):
        super().clean()
        if self.owner_id and self.seamstress_id and self.seamstress.owner_id != self.owner_id:
            raise ValidationError({"seamstress": "La couturière doit appartenir au même sous-traitant."})
        if self.status == self.Status.CANCELLED:
            if not self.cancelled_at:
                raise ValidationError({"cancelled_at": "Une annulation doit avoir une date."})
            if not self.cancelled_by_id:
                raise ValidationError({"cancelled_by": "Une annulation doit identifier son auteur."})
        elif self.cancelled_at or self.cancelled_by_id or self.cancellation_reason:
            raise ValidationError("Les informations d'annulation ne peuvent être utilisées que pour un paiement annulé.")
        if self.correction_of_id:
            if self.correction_of.owner_id != self.owner_id:
                raise ValidationError({"correction_of": "La correction doit appartenir au même sous-traitant."})
            if self.correction_of.seamstress_id != self.seamstress_id:
                raise ValidationError({"correction_of": "La correction doit concerner la même couturière."})

    def save(self, *args, **kwargs):
        if self.pk:
            original = type(self).objects.only(
                "owner_id", "seamstress_id", "amount", "paid_on",
                "kind", "method",
            ).get(pk=self.pk)
            protected_values = (
                "owner_id", "seamstress_id", "amount", "paid_on", "kind", "method",
            )
            for field_name in protected_values:
                if getattr(self, field_name) != getattr(original, field_name):
                    raise ValidationError(
                        "Un paiement enregistré ne peut pas être modifié silencieusement; "
                        "utilisez une annulation ou une correction."
                    )
        self.full_clean()
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError(
            "Un paiement ne peut pas être supprimé; utilisez une annulation ou une correction."
        )