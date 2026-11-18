from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Sum
from django.utils import timezone

from apps.finance.models import SeamstressRate
from apps.workshop.models import (
    Assignment,
    Delivery,
    DeliveryLine,
    Production,
    Return,
    Transfer,
    WorkOrderLine,
)


def _positive_quantity(quantity):
    if quantity <= 0:
        raise ValidationError("La quantité doit être supérieure à zéro.")


def _sum(queryset, field="quantity"):
    return queryset.aggregate(total=Sum(field))["total"] or 0


@transaction.atomic
def create_assignment(*, owner, order_line_id, seamstress, quantity, assigned_on, **kwargs):
    _positive_quantity(quantity)
    line = WorkOrderLine.objects.select_for_update().get(owner=owner, pk=order_line_id)
    if seamstress.owner_id != owner.pk:
        raise ValidationError("Cette couturière n'appartient pas à votre espace.")
    already_assigned = _sum(Assignment.objects.filter(
        owner=owner, order_line=line, created_transfer__isnull=True,
    ))
    if already_assigned + quantity > line.quantity_received:
        raise ValidationError("La quantité affectée dépasserait la quantité reçue.")
    return Assignment.objects.create(
        owner=owner, order_line=line, seamstress=seamstress,
        quantity=quantity, assigned_on=assigned_on, **kwargs,
    )


@transaction.atomic
def record_production(*, owner, assignment_id, quantity, produced_on=None, comments=""):
    _positive_quantity(quantity)
    assignment = Assignment.objects.select_for_update().get(owner=owner, pk=assignment_id)
    produced = _sum(Production.objects.filter(owner=owner, assignment=assignment))
    transferred = _sum(Transfer.objects.filter(owner=owner, source_assignment=assignment))
    if produced + transferred + quantity > assignment.quantity:
        raise ValidationError("La production dépasse la quantité encore sous responsabilité.")
    return Production.objects.create(
        owner=owner, assignment=assignment, quantity=quantity,
        produced_on=produced_on or timezone.localdate(), comments=comments,
    )


@transaction.atomic
def transfer_unsewn(*, owner, source_assignment_id, destination_seamstress, quantity, transferred_on=None, comments=""):
    _positive_quantity(quantity)
    source = Assignment.objects.select_for_update().get(owner=owner, pk=source_assignment_id)
    if destination_seamstress.owner_id != owner.pk:
        raise ValidationError("Cette couturière n'appartient pas à votre espace.")
    if destination_seamstress.pk == source.seamstress_id:
        raise ValidationError("La couturière destinataire doit être différente de la source.")
    produced = _sum(Production.objects.filter(owner=owner, assignment=source))
    transferred = _sum(Transfer.objects.filter(owner=owner, source_assignment=source))
    unsewn_in_custody = source.quantity - produced - transferred
    if quantity > unsewn_in_custody:
        raise ValidationError("Le transfert dépasse les pièces non cousues encore détenues.")
    destination_assignment = Assignment.objects.create(
        owner=owner, order_line=source.order_line, seamstress=destination_seamstress,
        quantity=quantity, assigned_on=transferred_on or timezone.localdate(),
        expected_return_on=source.expected_return_on,
        comments=f"Transfert de {source.seamstress.full_name}. {comments}".strip(),
    )
    transfer = Transfer.objects.create(
        owner=owner, source_assignment=source,
        destination_assignment=destination_assignment,
        destination_seamstress=destination_seamstress, quantity=quantity,
        transferred_on=transferred_on or timezone.localdate(), comments=comments,
    )
    return transfer


@transaction.atomic
def record_return(*, owner, assignment_id, quantity, returned_on=None, comments=""):
    _positive_quantity(quantity)
    assignment = Assignment.objects.select_for_update().select_related(
        "order_line__article", "seamstress"
    ).get(owner=owner, pk=assignment_id)
    produced = _sum(Production.objects.filter(owner=owner, assignment=assignment))
    returned = _sum(Return.objects.filter(owner=owner, assignment=assignment))
    if returned + quantity > produced:
        raise ValidationError("Les pièces retournées ne peuvent pas dépasser les pièces cousues.")
    custom_rate = SeamstressRate.objects.filter(
        owner=owner, article_id=assignment.order_line.article_id,
        seamstress_id=assignment.seamstress_id,
    ).values_list("unit_rate", flat=True).first()
    rate = custom_rate if custom_rate is not None else assignment.order_line.default_rate_snapshot
    return Return.objects.create(
        owner=owner, assignment=assignment, quantity=quantity,
        returned_on=returned_on or timezone.localdate(), unit_rate=rate,
        total_amount=(Decimal(quantity) * rate).quantize(Decimal("0.01")),
        comments=comments,
    )


@transaction.atomic
def record_delivery(*, owner, delivered_on, lines, reference="", comments="", document=None):
    """Create a delivery from pieces physically held by the subcontractor."""
    normalized = {}
    for order_line_id, quantity in lines:
        _positive_quantity(quantity)
        normalized[order_line_id] = normalized.get(order_line_id, 0) + quantity
    locked_lines = {
        line.pk: line for line in WorkOrderLine.objects.select_for_update().filter(
            owner=owner, pk__in=normalized
        )
    }
    if len(locked_lines) != len(normalized):
        raise ValidationError("Une ou plusieurs lignes de bon sont introuvables.")
    for line_id, quantity in normalized.items():
        line = locked_lines[line_id]
        returned = _sum(Return.objects.filter(owner=owner, assignment__order_line=line))
        delivered = _sum(DeliveryLine.objects.filter(owner=owner, order_line=line))
        if delivered + quantity > returned:
            raise ValidationError("La livraison dépasse le stock physiquement disponible.")
    delivery = Delivery.objects.create(
        owner=owner, delivered_on=delivered_on, reference=reference,
        comments=comments, document=document,
    )
    DeliveryLine.objects.bulk_create([
        DeliveryLine(owner=owner, delivery=delivery, order_line=locked_lines[line_id], quantity=quantity)
        for line_id, quantity in normalized.items()
    ])
    return delivery