from decimal import Decimal

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import MinValueValidator
from django.db import models
from django.db.models import Q


class OwnedModel(models.Model):
    owner = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        abstract = True

    def save(self, *args, **kwargs):
        self.full_clean()
        return super().save(*args, **kwargs)


class HistoricalMovementModel(OwnedModel):
    is_cancelled = models.BooleanField(default=False)
    cancelled_at = models.DateField(null=True, blank=True)
    cancelled_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        related_name="+",
        on_delete=models.PROTECT,
    )
    cancellation_reason = models.TextField(blank=True)

    class Meta:
        abstract = True

    def clean(self):
        super().clean()
        if self.is_cancelled:
            if not self.cancelled_at:
                raise ValidationError({"cancelled_at": "Une annulation doit avoir une date."})
            if not self.cancelled_by_id:
                raise ValidationError({"cancelled_by": "Une annulation doit identifier son auteur."})
        elif self.cancelled_at or self.cancelled_by_id or self.cancellation_reason:
            raise ValidationError("Les informations d'annulation ne peuvent être utilisées que si le mouvement est annulé.")

    def save(self, *args, **kwargs):
        if self.pk:
            original = type(self).objects.get(pk=self.pk)
            allowed_cancellation_fields = {
                "is_cancelled",
                "cancelled_at",
                "cancelled_by_id",
                "cancellation_reason",
            }
            for field in self._meta.concrete_fields:
                if field.name in allowed_cancellation_fields or field.attname in allowed_cancellation_fields:
                    continue
                if field.attname == "updated_at":
                    continue
                if getattr(self, field.attname) != getattr(original, field.attname):
                    raise ValidationError(
                        "Un mouvement historique ne peut pas être modifié silencieusement; "
                        "utilisez une annulation ou une correction."
                    )
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError(
            "Un mouvement historique ne peut pas être supprimé; "
            "utilisez une annulation ou une correction."
        )


class Article(OwnedModel):
    reference = models.CharField(max_length=80)
    name = models.CharField(max_length=180)
    description = models.TextField(blank=True)
    default_sewing_rate = models.DecimalField(
        max_digits=10, decimal_places=2, default=Decimal("0.00"),
        validators=[MinValueValidator(Decimal("0.00"))],
    )
    is_active = models.BooleanField(default=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["owner", "reference"], name="uniq_article_owner_ref"),
            models.CheckConstraint(
                condition=Q(default_sewing_rate__gte=Decimal("0.00")),
                name="chk_article_rate_not_negative",
            ),
        ]
        indexes = [models.Index(fields=["owner", "name"])]
        ordering = ["reference"]

    def __str__(self):
        return f"{self.reference} - {self.name}"


class WorkOrder(OwnedModel):
    class Status(models.TextChoices):
        OPEN = "open", "En cours"
        COMPLETED = "completed", "Terminé"
        CLOSED = "closed", "Clôturé"

    number = models.CharField(max_length=80)
    received_on = models.DateField()
    due_on = models.DateField(null=True, blank=True)
    closed_on = models.DateField(null=True, blank=True)
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.OPEN)
    notes = models.TextField(blank=True)
    is_archived = models.BooleanField(default=False)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["owner", "number"], name="uniq_order_owner_number"),
            models.CheckConstraint(
                condition=Q(due_on__isnull=True) | Q(due_on__gte=models.F("received_on")),
                name="chk_work_order_due_not_before_received",
            ),
        ]
        indexes = [models.Index(fields=["owner", "status", "due_on"])]
        ordering = ["-received_on", "number"]

    def __str__(self):
        return self.number


class WorkOrderLine(OwnedModel):
    order = models.ForeignKey(WorkOrder, related_name="lines", on_delete=models.PROTECT)
    article = models.ForeignKey(Article, related_name="order_lines", null=True, blank=True, on_delete=models.PROTECT)
    reference_snapshot = models.CharField(max_length=80)
    name_snapshot = models.CharField(max_length=180)
    description = models.TextField(blank=True)
    quantity_received = models.PositiveIntegerField(validators=[MinValueValidator(1)])
    due_on = models.DateField(null=True, blank=True)
    default_rate_snapshot = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal("0.00"))

    class Meta:
        indexes = [models.Index(fields=["owner", "order"]), models.Index(fields=["owner", "reference_snapshot"])]
        constraints = [
            models.CheckConstraint(
                condition=Q(quantity_received__gt=0),
                name="chk_work_order_line_quantity_positive",
            ),
            models.CheckConstraint(
                condition=Q(default_rate_snapshot__gte=Decimal("0.00")),
                name="chk_work_order_line_rate_not_negative",
            ),
        ]

    def __str__(self):
        return f"{self.order.number} / {self.reference_snapshot}"

    def clean(self):
        super().clean()
        if self.order_id and self.owner_id and self.order.owner_id != self.owner_id:
            raise ValidationError({"order": "Le bon doit appartenir au même sous-traitant."})
        if self.article_id and self.owner_id and self.article.owner_id != self.owner_id:
            raise ValidationError({"article": "L'article doit appartenir au même sous-traitant."})


class Seamstress(OwnedModel):
    full_name = models.CharField(max_length=180)
    phone = models.CharField(max_length=32, blank=True)
    address = models.CharField(max_length=240, blank=True)
    notes = models.TextField(blank=True)
    is_active = models.BooleanField(default=True)

    class Meta:
        indexes = [models.Index(fields=["owner", "full_name"]), models.Index(fields=["owner", "is_active"])]
        ordering = ["full_name"]

    def __str__(self):
        return self.full_name


class Assignment(HistoricalMovementModel):
    """A quantity allocation; transfer-created allocations point to their transfer."""

    order_line = models.ForeignKey(WorkOrderLine, related_name="assignments", on_delete=models.PROTECT)
    seamstress = models.ForeignKey(Seamstress, related_name="assignments", on_delete=models.PROTECT)
    quantity = models.PositiveIntegerField(validators=[MinValueValidator(1)])
    assigned_on = models.DateField()
    expected_return_on = models.DateField(null=True, blank=True)
    comments = models.TextField(blank=True)

    class Meta:
        indexes = [models.Index(fields=["owner", "seamstress", "order_line"])]
        constraints = [
            models.CheckConstraint(
                condition=Q(quantity__gt=0),
                name="chk_assignment_quantity_positive",
            ),
        ]

    @property
    def is_transfer_destination(self):
        return hasattr(self, "created_transfer")

    def clean(self):
        super().clean()
        if self.order_line_id and self.owner_id and self.order_line.owner_id != self.owner_id:
            raise ValidationError({"order_line": "La ligne de bon doit appartenir au même sous-traitant."})
        if self.seamstress_id and self.owner_id and self.seamstress.owner_id != self.owner_id:
            raise ValidationError({"seamstress": "La couturière doit appartenir au même sous-traitant."})

class Transfer(HistoricalMovementModel):
    source_assignment = models.ForeignKey(Assignment, related_name="transfers_out", on_delete=models.PROTECT)
    destination_assignment = models.OneToOneField(
        Assignment, related_name="created_transfer", on_delete=models.PROTECT
    )
    destination_seamstress = models.ForeignKey(Seamstress, related_name="transfers_in", on_delete=models.PROTECT)
    quantity = models.PositiveIntegerField(validators=[MinValueValidator(1)])
    transferred_on = models.DateField()
    comments = models.TextField(blank=True)

    class Meta:
        indexes = [models.Index(fields=["owner", "transferred_on"])]
        constraints = [
            models.CheckConstraint(
                condition=Q(quantity__gt=0),
                name="chk_transfer_quantity_positive",
            ),
        ]

    def clean(self):
        super().clean()
        if self.owner_id:
            if self.source_assignment_id and self.source_assignment.owner_id != self.owner_id:
                raise ValidationError({"source_assignment": "L'affectation source doit appartenir au même sous-traitant."})
            if self.destination_assignment_id and self.destination_assignment.owner_id != self.owner_id:
                raise ValidationError({"destination_assignment": "L'affectation destination doit appartenir au même sous-traitant."})
            if self.destination_seamstress_id and self.destination_seamstress.owner_id != self.owner_id:
                raise ValidationError({"destination_seamstress": "La couturière destination doit appartenir au même sous-traitant."})
        if self.source_assignment_id and self.destination_assignment_id:
            if self.source_assignment_id == self.destination_assignment_id:
                raise ValidationError("Les affectations source et destination doivent être différentes.")
            if self.source_assignment.order_line_id != self.destination_assignment.order_line_id:
                raise ValidationError("Les affectations doivent concerner la même ligne de bon.")
            if self.destination_assignment.quantity != self.quantity:
                raise ValidationError({"destination_assignment": "La quantité destination doit correspondre au transfert."})
            if self.destination_assignment.seamstress_id != self.destination_seamstress_id:
                raise ValidationError({"destination_seamstress": "La destination doit correspondre à l'affectation créée."})

class Production(HistoricalMovementModel):
    assignment = models.ForeignKey(Assignment, related_name="production_entries", on_delete=models.PROTECT)
    quantity = models.PositiveIntegerField(validators=[MinValueValidator(1)])
    produced_on = models.DateField()
    comments = models.TextField(blank=True)

    class Meta:
        indexes = [models.Index(fields=["owner", "produced_on"])]
        constraints = [
            models.CheckConstraint(
                condition=Q(quantity__gt=0),
                name="chk_production_quantity_positive",
            ),
        ]

    def clean(self):
        super().clean()
        if self.assignment_id and self.owner_id and self.assignment.owner_id != self.owner_id:
            raise ValidationError({"assignment": "L'affectation doit appartenir au même sous-traitant."})

class Return(HistoricalMovementModel):
    """Physical receipt from a seamstress; rate and total are immutable snapshots."""

    assignment = models.ForeignKey(Assignment, related_name="returns", on_delete=models.PROTECT)
    quantity = models.PositiveIntegerField(validators=[MinValueValidator(1)])
    returned_on = models.DateField()
    unit_rate = models.DecimalField(max_digits=10, decimal_places=2)
    total_amount = models.DecimalField(max_digits=12, decimal_places=2)
    comments = models.TextField(blank=True)

    class Meta:
        indexes = [models.Index(fields=["owner", "returned_on"])]
        constraints = [
            models.CheckConstraint(
                condition=Q(quantity__gt=0),
                name="chk_return_quantity_positive",
            ),
            models.CheckConstraint(
                condition=Q(unit_rate__gte=Decimal("0.00")),
                name="chk_return_unit_rate_not_negative",
            ),
            models.CheckConstraint(
                condition=Q(total_amount__gte=Decimal("0.00")),
                name="chk_return_total_not_negative",
            ),
        ]

    def clean(self):
        super().clean()
        if self.assignment_id and self.owner_id and self.assignment.owner_id != self.owner_id:
            raise ValidationError({"assignment": "L'affectation doit appartenir au même sous-traitant."})
        if self.total_amount is not None and self.unit_rate is not None and self.quantity:
            expected = (Decimal(self.quantity) * self.unit_rate).quantize(Decimal("0.01"))
            if self.total_amount != expected:
                raise ValidationError({"total_amount": "Le montant doit être égal à quantité x tarif historique."})


class Delivery(HistoricalMovementModel):
    reference = models.CharField(max_length=80, blank=True)
    delivered_on = models.DateField()
    document = models.FileField(upload_to="deliveries/%Y/%m/", blank=True)
    comments = models.TextField(blank=True)

    class Meta:
        indexes = [models.Index(fields=["owner", "delivered_on"])]


class DeliveryLine(OwnedModel):
    delivery = models.ForeignKey(Delivery, related_name="lines", on_delete=models.PROTECT)
    order_line = models.ForeignKey(WorkOrderLine, related_name="delivery_lines", on_delete=models.PROTECT)
    quantity = models.PositiveIntegerField(validators=[MinValueValidator(1)])

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=Q(quantity__gt=0),
                name="chk_delivery_line_quantity_positive",
            ),
            models.UniqueConstraint(fields=["delivery", "order_line"], name="uniq_delivery_line_per_order"),
        ]

    def clean(self):
        super().clean()
        if self.owner_id:
            if self.delivery_id and self.delivery.owner_id != self.owner_id:
                raise ValidationError({"delivery": "La livraison doit appartenir au même sous-traitant."})
            if self.order_line_id and self.order_line.owner_id != self.owner_id:
                raise ValidationError({"order_line": "La ligne de bon doit appartenir au même sous-traitant."})


class AuditLog(OwnedModel):
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        related_name="audit_actions",
        on_delete=models.SET_NULL,
    )
    entity_type = models.CharField(max_length=80)
    entity_id = models.PositiveBigIntegerField()
    action = models.CharField(max_length=32)
    details = models.JSONField(default=dict)

    class Meta:
        indexes = [models.Index(fields=["owner", "entity_type", "entity_id"])]

    def clean(self):
        super().clean()
        if self.actor_id and self.owner_id and self.actor_id != self.owner_id:
            raise ValidationError({"actor": "L'auteur doit appartenir à l'espace audité."})

    def save(self, *args, **kwargs):
        if self.pk:
            raise ValidationError("Un journal d'audit ne peut pas être modifié.")
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError("Un journal d'audit ne peut pas être supprimé.")