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
            ("assign_srp_managers", "Can hand out and take away the SRP Manager role"),
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


class SrpTag(models.Model):
    """A label on requests ("BLOPS SRP", "SANCTION SRP"). Who may look at the requests that carry it is set here: a member of one of the viewer groups sees those requests
    (and only those) in the inbox without being an SRP manager, so the people who run one kind of SRP can follow it easily."""

    name = models.CharField(max_length=40, unique=True)
    slug = models.SlugField(max_length=50, unique=True)
    color = models.CharField(max_length=7, default="#6c757d", help_text=_("Badge colour as #rrggbb."))
    description = models.CharField(max_length=200, blank=True, default="")
    viewer_groups = models.ManyToManyField(
        "auth.Group", blank=True, related_name="+",
        help_text=_("Members of these groups see the requests with this tag in the SRP inbox (read only; deciding stays with the SRP managers)."))
    is_active = models.BooleanField(default=True, help_text=_("An inactive tag is not given to new requests and gives nobody access."))
    order = models.PositiveSmallIntegerField(default=100, help_text=_("Lower comes first in lists and in the filter."))

    class Meta:
        ordering = ["order", "name"]

    def __str__(self) -> str:
        return self.name


class SrpTagRule(models.Model):
    """What gives a request its tags without anybody doing anything: a word in the mail subject (a hunter writes HUNT), a word in the mail text, or the ship group of the loss
    (Black Ops). One rule can give several tags. Tags are only ever ADDED by rules."""

    class Kind(models.TextChoices):
        SUBJECT = "subject", _("Word in the mail subject")
        BODY = "body", _("Word in the mail text")
        HULL = "hull", _("Ship group of the loss (e.g. Black Ops)")

    kind = models.CharField(max_length=8, choices=Kind.choices, default=Kind.SUBJECT)
    keyword = models.CharField(max_length=80, help_text=_("A whole word or phrase, any case: HUNT matches 'hunt' and 'HUNT loss', not 'hunters'. For a ship group: its exact name."))
    tags = models.ManyToManyField(SrpTag, related_name="rules", help_text=_("Every tag chosen here is given to a request the rule matches."))
    is_active = models.BooleanField(default=True)
    note = models.CharField(max_length=200, blank=True, default="")

    class Meta:
        ordering = ["kind", "keyword", "id"]

    def __str__(self) -> str:
        return f"{self.get_kind_display()}: {self.keyword}"


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
    tags = models.ManyToManyField(SrpTag, blank=True, related_name="requests")
    tags_edited = models.BooleanField(default=False, db_default=False, help_text=_("Somebody changed the tags by hand: the rules leave this request alone when they are applied again."))

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
    def reason_text(self) -> str:
        """What to type as the Reason of the in-game payment: it names this request, so the wallet booking
        can be tied back to it (yagas_finance files it under SRP and the inbox shows the request as paid)."""
        return f"SRP #{self.pk}"

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
