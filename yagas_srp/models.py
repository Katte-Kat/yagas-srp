from django.conf import settings
from django.db import models
from django.utils.translation import gettext_lazy as _


class SrpPermissions(models.Model):
    """Meta model for app permissions."""

    class Meta:
        managed = False
        default_permissions = ()
        permissions = (
            ("view_requests", "Can view the SRP request inbox"),
            ("approve_requests", "Can approve or deny SRP requests"),
            ("manage_receiver", "Can link the SRP mail receiver character"),
        )


class MailReceiver(models.Model):
    """The character whose in-game mailbox is polled for SRP requests."""

    character_id = models.BigIntegerField(unique=True)
    character_name = models.CharField(max_length=150)
    is_active = models.BooleanField(default=True)
    added_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    last_polled_at = models.DateTimeField(null=True, blank=True)
    last_error = models.TextField(blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self) -> str:
        return self.character_name


class SrpRequest(models.Model):
    """One inbound mail, with the killmail it links to already resolved and valued."""

    class Status(models.TextChoices):
        PENDING = "pending", _("Pending")
        APPROVED = "approved", _("Approved")
        DENIED = "denied", _("Denied")
        INVALID = "invalid", _("Invalid")
        ARCHIVED = "archived", _("Archived")  # old mails imported from the mailbox history, not up for a decision

    receiver = models.ForeignKey(
        MailReceiver, on_delete=models.SET_NULL, null=True, blank=True, related_name="requests")
    mail_id = models.BigIntegerField(null=True, blank=True)
    sender_id = models.BigIntegerField(null=True, blank=True)
    sender_name = models.CharField(max_length=150, blank=True, default="")
    subject = models.CharField(max_length=255, blank=True, default="")
    body = models.TextField(blank=True, default="")
    received_at = models.DateTimeField()

    killmail_id = models.BigIntegerField(null=True, blank=True, db_index=True)
    killmail_hash = models.CharField(max_length=64, blank=True, default="")
    kill_time = models.DateTimeField(null=True, blank=True)
    victim_character_id = models.BigIntegerField(null=True, blank=True)
    victim_name = models.CharField(max_length=150, blank=True, default="")
    victim_corporation_id = models.BigIntegerField(null=True, blank=True)
    victim_corporation_name = models.CharField(max_length=150, blank=True, default="")
    ship_type_id = models.BigIntegerField(null=True, blank=True)
    ship_type_name = models.CharField(max_length=150, blank=True, default="")
    solar_system_id = models.BigIntegerField(null=True, blank=True)
    solar_system_name = models.CharField(max_length=150, blank=True, default="")

    ship_value = models.DecimalField(max_digits=20, decimal_places=0, default=0)
    fit_value = models.DecimalField(max_digits=20, decimal_places=0, default=0)
    cargo_value = models.DecimalField(max_digits=20, decimal_places=0, default=0)
    total_value = models.DecimalField(max_digits=20, decimal_places=0, default=0)
    unpriced_count = models.PositiveIntegerField(default=0)
    ship_price_source = models.CharField(max_length=8, blank=True, default="")
    priced_at = models.DateTimeField(null=True, blank=True)

    status = models.CharField(max_length=10, choices=Status.choices, default=Status.PENDING, db_index=True)
    validation_flags = models.JSONField(default=list, blank=True)

    reviewed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    reviewed_at = models.DateTimeField(null=True, blank=True)
    review_note = models.TextField(blank=True, default="")
    approved_amount = models.DecimalField(max_digits=20, decimal_places=0, null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-received_at", "-id"]
        constraints = [
            models.UniqueConstraint(fields=["receiver", "mail_id"], name="yagas_srp_unique_mail_per_receiver"),
        ]

    def __str__(self) -> str:
        return f"#{self.pk} {self.ship_type_name or 'no killmail'} ({self.status})"

    @property
    def payout_value(self):
        """What would be (or was) paid: the approved amount once approved, else the computed total."""
        if self.status == self.Status.APPROVED and self.approved_amount is not None:
            return self.approved_amount
        return self.total_value

    @property
    def pilot_name(self) -> str:
        """The lost pilot as shown in lists; the mail sender when the killmail could not be read."""
        return self.victim_name or self.sender_name


class SrpRequestItem(models.Model):
    """One killmail line: hull excluded (it lives on the request), everything else the ship carried."""

    request = models.ForeignKey(SrpRequest, on_delete=models.CASCADE, related_name="items")
    type_id = models.BigIntegerField()
    type_name = models.CharField(max_length=150, blank=True, default="")
    flag = models.IntegerField()
    slot_group = models.CharField(max_length=12)
    quantity_destroyed = models.PositiveIntegerField(default=0)
    quantity_dropped = models.PositiveIntegerField(default=0)
    unit_price = models.DecimalField(max_digits=20, decimal_places=0, null=True, blank=True)
    value = models.DecimalField(max_digits=20, decimal_places=0, default=0)
    price_source = models.CharField(max_length=8, blank=True, default="")
    counted = models.BooleanField(default=False, help_text="Counts towards the SRP value (fitted slots, rigs, drones)")

    class Meta:
        ordering = ["id"]

    @property
    def quantity(self) -> int:
        return self.quantity_destroyed + self.quantity_dropped
