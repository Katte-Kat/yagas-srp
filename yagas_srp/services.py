"""Turning one inbound in-game mail into a valued SRP request.

Everything that can fail transiently (ESI 5xx, network) raises, so the caller
leaves the mail unrecorded and simply retries on the next poll. Everything
that is permanently wrong with a mail (no killmail link, bad hash, duplicate)
is recorded as an INVALID request instead, so a director can see why it was
not queued rather than the mail silently vanishing.
"""
import html
import logging
import re
from datetime import timedelta
from decimal import ROUND_HALF_UP, Decimal

from esi.exceptions import HTTPClientError

from django.conf import settings
from django.core.cache import cache
from django.db import transaction
from django.utils import timezone
from django.utils.html import strip_tags

from yagas_srp import __version__

logger = logging.getLogger(__name__)

MAIL_SCOPE = "esi-mail.read_mail.v1"

KILLREPORT_RE = re.compile(r"killReport:(\d+):([0-9a-fA-F]{40})")
ESI_KILLMAIL_RE = re.compile(r"killmails/(\d+)/([0-9a-fA-F]{40})")
ZKILL_RE = re.compile(r"zkillboard\.com/kill/(\d+)")

PRICE_CACHE_KEY = "yagas_srp_market_prices"
PRICE_CACHE_SECONDS = 3600

# Jita 4-4 (Caldari Navy Assembly Plant) order book via Fuzzwork's public aggregates.
FUZZWORK_URL = "https://market.fuzzwork.co.uk/aggregates/"
JITA_STATION_ID = 60003760
JITA_CACHE_SECONDS = 3600
FUZZWORK_CHUNK = 150
# Where a price came from: the real split, one side of the book only, or CCP's ESI average.
SOURCE_SPLIT, SOURCE_BUY, SOURCE_SELL, SOURCE_ESI = "split", "buy", "sell", "esi"
CAPSULE_TYPE_IDS = {670, 33328}
FIT_GROUPS = {"high", "mid", "low", "rig", "subsystem", "drone", "fighter"}

_ISK = Decimal("1")  # EVE has no sub-ISK amounts; everything is whole ISK


def esi_provider(tags):
    from esi.openapi_clients import ESIClientProvider

    from allianceauth.eveonline import __esi_compatibility_date__

    return ESIClientProvider(
        __esi_compatibility_date__, "yagas-srp", __version__,
        "https://github.com/Katte-Kat/yagas-srp", tags=tags,
    )


def slot_group(flag: int) -> str:
    """ESI killmail inventory flag -> which part of the ship the item sat in."""
    if 27 <= flag <= 34:
        return "high"
    if 19 <= flag <= 26:
        return "mid"
    if 11 <= flag <= 18:
        return "low"
    if 92 <= flag <= 94:
        return "rig"
    if 125 <= flag <= 132:
        return "subsystem"
    if flag == 87:
        return "drone"
    if flag == 158 or 159 <= flag <= 163:
        return "fighter"
    if flag == 89:
        return "implant"
    if flag == 5:
        return "cargo"
    return "other"


def html_to_text(raw: str) -> str:
    text = re.sub(r"(?i)<br\s*/?>", "\n", raw or "")
    return html.unescape(strip_tags(text)).strip()


def _zkill_hash(killmail_id: int):
    """zKillboard links carry no hash; ask zKill for it (the in-game killReport
    links, which every in-game 'share killmail' produces, don't need this)."""
    import httpx

    contact = getattr(settings, "ESI_USER_CONTACT_EMAIL", "") or "unknown"
    response = httpx.get(
        f"https://zkillboard.com/api/killID/{killmail_id}/",
        headers={"User-Agent": f"yagas-srp {__version__} (contact: {contact})"},
        timeout=15,
    )
    response.raise_for_status()
    data = response.json()
    return data[0]["zkb"]["hash"] if data else None


def extract_killmail_ref(raw_body: str):
    """Returns (killmail_id, hash_or_None), or None if the mail links no killmail."""
    for pattern in (KILLREPORT_RE, ESI_KILLMAIL_RE):
        match = pattern.search(raw_body or "")
        if match:
            return int(match.group(1)), match.group(2).lower()
    match = ZKILL_RE.search(raw_body or "")
    if match:
        killmail_id = int(match.group(1))
        try:
            return killmail_id, _zkill_hash(killmail_id)
        except Exception:
            logger.warning("yagas_srp: could not resolve zKillboard hash for %s", killmail_id, exc_info=True)
            return killmail_id, None
    return None


def fetch_prices(fresh: bool = False) -> dict:
    """CCP's public price feed (same one yagas_finance values hangars with)."""
    prices = None if fresh else cache.get(PRICE_CACHE_KEY)
    if prices:
        return prices
    esi = esi_provider(["Market"])
    rows, _resp = esi.client.Market.GetMarketsPrices().result(return_response=True, force_refresh=True)
    prices = {
        row.type_id: float(row.average_price or row.adjusted_price)
        for row in rows
        if (row.average_price or row.adjusted_price)
    }
    cache.set(PRICE_CACHE_KEY, prices, PRICE_CACHE_SECONDS)
    return prices


def _jita_cache_key(type_id: int) -> str:
    return f"yagas_srp_jita_{type_id}"


def fetch_jita_prices(type_ids, fresh: bool = False) -> dict:
    """{type_id: (price, source)} from Fuzzwork's Jita 4-4 order book (0, "") if nothing is listed.

    Split = (highest buy + lowest sell) / 2, the usual definition. With orders on one
    side only there is no split, so that side's price is used and marked as such.
    Network/HTTP failures raise: the caller must not silently switch price basis.
    Prices are cached for an hour; `fresh=True` skips reading that cache (it still refills it).
    """
    import httpx

    wanted = sorted({int(i) for i in type_ids if i})
    result = {} if fresh else cache.get_many([_jita_cache_key(i) for i in wanted])
    result = {int(key.rsplit("_", 1)[1]): value for key, value in result.items()}
    missing = [i for i in wanted if i not in result]

    contact = getattr(settings, "ESI_USER_CONTACT_EMAIL", "") or "unknown"
    for start in range(0, len(missing), FUZZWORK_CHUNK):
        chunk = missing[start:start + FUZZWORK_CHUNK]
        response = httpx.get(
            FUZZWORK_URL,
            params={"station": JITA_STATION_ID, "types": ",".join(map(str, chunk))},
            headers={"User-Agent": f"yagas-srp {__version__} (contact: {contact})"},
            timeout=20,
        )
        response.raise_for_status()
        data = response.json()
        loaded = {}
        for type_id in chunk:
            book = data.get(str(type_id)) or {}
            buy = float((book.get("buy") or {}).get("max") or 0)
            sell = float((book.get("sell") or {}).get("min") or 0)
            if buy and sell:
                loaded[type_id] = ((buy + sell) / 2, SOURCE_SPLIT)
            elif sell:
                loaded[type_id] = (sell, SOURCE_SELL)
            elif buy:
                loaded[type_id] = (buy, SOURCE_BUY)
            else:
                loaded[type_id] = (0, "")
        cache.set_many({_jita_cache_key(k): v for k, v in loaded.items()}, JITA_CACHE_SECONDS)
        result.update(loaded)
    return result


def price_types(type_ids, fresh: bool = False) -> dict:
    """{type_id: (price, source)} for everything that has a price: Jita split first,
    CCP's ESI average only for types with no Jita orders at all."""
    jita = fetch_jita_prices(type_ids, fresh=fresh)
    esi = None
    priced = {}
    for type_id in {int(i) for i in type_ids if i}:
        price, source = jita.get(type_id, (0, ""))
        if not price:
            if esi is None:
                esi = fetch_prices(fresh=fresh)
            if esi.get(type_id):
                price, source = esi[type_id], SOURCE_ESI
        if price:
            priced[type_id] = (price, source)
    return priced


def resolve_names(ids) -> dict:
    ids = sorted({i for i in ids if i})
    if not ids:
        return {}
    esi = esi_provider(["Universe"])
    names = {}
    for start in range(0, len(ids), 1000):
        chunk = ids[start:start + 1000]
        try:
            rows, _resp = esi.client.Universe.PostUniverseNames(body=chunk).result(
                return_response=True, force_refresh=True)
            names.update({row.id: row.name for row in rows})
        except HTTPClientError:
            # ESI fails the whole batch if a single id is unknown; fall back to one by one
            for single in chunk:
                try:
                    rows, _resp = esi.client.Universe.PostUniverseNames(body=[single]).result(
                        return_response=True, force_refresh=True)
                    names.update({row.id: row.name for row in rows})
                except HTTPClientError:
                    continue
    return names


def _money(value) -> Decimal:
    return Decimal(str(value)).quantize(_ISK, rounding=ROUND_HALF_UP)


def _valuate(ship_type_id, lines, fresh: bool = False) -> dict:
    """Values the hull and `lines` [(type_id, quantity, slot_group, counted)] at current prices."""
    prices = price_types({ship_type_id, *(line[0] for line in lines)}, fresh=fresh)
    ship_price, ship_source = prices.get(ship_type_id, (0, ""))
    ship_value = _money(ship_price)
    unpriced = 0 if ship_value else 1
    fit_value = cargo_value = Decimal("0")
    priced = []
    for type_id, quantity, group, counted in lines:
        price, source = prices.get(type_id, (0, ""))
        value = _money(quantity * price) if price else Decimal("0")
        if not price and group != "implant":
            unpriced += 1
        if counted:
            fit_value += value
        elif group in ("cargo", "other"):
            cargo_value += value
        priced.append({"unit_price": _money(price) if price else None, "value": value, "source": source})
    sources = [ship_source] + [row["source"] for row in priced if row["value"]]
    return {
        "ship_value": ship_value, "ship_source": ship_source, "fit_value": _money(fit_value),
        "cargo_value": _money(cargo_value), "total_value": _money(ship_value + fit_value),
        "unpriced": unpriced, "lines": priced,
        "esi_fallback": sources.count(SOURCE_ESI),
        "one_sided": sources.count(SOURCE_BUY) + sources.count(SOURCE_SELL),
    }


PRICE_FLAG_PREFIX = "Pricing: "


def _is_price_flag(flag: str) -> bool:
    return flag.startswith(PRICE_FLAG_PREFIX) or "market price" in flag  # second form: pre-Jita rows


def _price_flags(valuation: dict) -> list:
    flags = []
    if valuation["unpriced"]:
        flags.append(f"{PRICE_FLAG_PREFIX}{valuation['unpriced']} item(s) without any market price -- value is understated")
    if valuation["esi_fallback"]:
        flags.append(f"{PRICE_FLAG_PREFIX}{valuation['esi_fallback']} item(s) have no Jita orders and use CCP's ESI average")
    if valuation["one_sided"]:
        flags.append(f"{PRICE_FLAG_PREFIX}{valuation['one_sided']} item(s) have Jita orders on one side only (no real split)")
    return flags


def reprice_request(srp) -> None:
    """Re-values a stored request at the live Jita split (bypassing the hourly cache); killmail data stays as it was."""
    from yagas_srp.models import SrpRequestItem

    items = list(srp.items.all())
    valuation = _valuate(
        srp.ship_type_id, [(i.type_id, i.quantity, i.slot_group, i.counted) for i in items], fresh=True)
    for item, row in zip(items, valuation["lines"]):
        item.unit_price, item.value, item.price_source = row["unit_price"], row["value"], row["source"]
    srp.ship_value, srp.fit_value = valuation["ship_value"], valuation["fit_value"]
    srp.cargo_value, srp.total_value = valuation["cargo_value"], valuation["total_value"]
    srp.unpriced_count, srp.ship_price_source = valuation["unpriced"], valuation["ship_source"]
    srp.priced_at = timezone.now()
    srp.validation_flags = [f for f in srp.validation_flags if not _is_price_flag(f)] + _price_flags(valuation)
    with transaction.atomic():
        SrpRequestItem.objects.bulk_update(items, ["unit_price", "value", "price_source"])
        srp.save(update_fields=[
            "ship_value", "fit_value", "cargo_value", "total_value", "unpriced_count",
            "ship_price_source", "priced_at", "validation_flags"])


def create_request_from_mail(*, receiver, mail_id, sender_id, subject, body_html, received_at):
    """Creates (and returns) the SrpRequest for one inbound mail."""
    from yagas_srp.models import SrpRequest, SrpRequestItem

    base = {
        "receiver": receiver, "mail_id": mail_id, "sender_id": sender_id,
        "subject": (subject or "")[:255], "body": html_to_text(body_html), "received_at": received_at,
    }

    def invalid(reason, **extra):
        sender_name = resolve_names([sender_id]).get(sender_id, "") if sender_id else ""
        return SrpRequest.objects.create(
            **base, sender_name=sender_name, status=SrpRequest.Status.INVALID,
            validation_flags=[reason], **extra)

    ref = extract_killmail_ref(body_html or "")
    if ref is None:
        return invalid("No killmail link found in the mail")
    killmail_id, killmail_hash = ref
    if not killmail_hash:
        return invalid(
            "Killmail hash missing (zKillboard link could not be resolved) -- "
            "ask the pilot to share the kill from the in-game combat log", killmail_id=killmail_id)

    duplicate = SrpRequest.objects.filter(killmail_id=killmail_id).exclude(
        status=SrpRequest.Status.INVALID).first()
    if duplicate is not None:
        return invalid(f"Duplicate: killmail already requested in #{duplicate.pk}",
                       killmail_id=killmail_id, killmail_hash=killmail_hash)

    esi = esi_provider(["Killmails"])
    try:
        killmail, _resp = esi.client.Killmails.GetKillmailsKillmailIdKillmailHash(
            killmail_id=killmail_id, killmail_hash=killmail_hash).result(
            return_response=True, force_refresh=True)
    except HTTPClientError:
        return invalid("Killmail not found at ESI (wrong link or hash)",
                       killmail_id=killmail_id, killmail_hash=killmail_hash)

    victim = killmail.victim
    victim_character_id = getattr(victim, "character_id", None)
    victim_corporation_id = getattr(victim, "corporation_id", None)
    ship_type_id = victim.ship_type_id
    kill_items = list(victim.items or [])

    lines = []
    for item in kill_items:
        group = slot_group(item.flag)
        lines.append((item.item_type_id, (item.quantity_destroyed or 0) + (item.quantity_dropped or 0),
                      group, group in FIT_GROUPS))
    valuation = _valuate(ship_type_id, lines)
    names = resolve_names(
        [sender_id, victim_character_id, victim_corporation_id, ship_type_id, killmail.solar_system_id]
        + [item.item_type_id for item in kill_items])

    rows = []
    for item, (_type_id, _qty, group, counted), priced in zip(kill_items, lines, valuation["lines"]):
        rows.append(SrpRequestItem(
            type_id=item.item_type_id, type_name=names.get(item.item_type_id, ""), flag=item.flag,
            slot_group=group, quantity_destroyed=item.quantity_destroyed or 0,
            quantity_dropped=item.quantity_dropped or 0, unit_price=priced["unit_price"],
            value=priced["value"], price_source=priced["source"], counted=counted))

    flags = _validation_flags(
        sender_id=sender_id, sender_name=names.get(sender_id, str(sender_id)),
        victim_character_id=victim_character_id, victim_name=names.get(victim_character_id, ""),
        victim_corporation_id=victim_corporation_id, kill_time=killmail.killmail_time,
        ship_type_id=ship_type_id) + _price_flags(valuation)

    with transaction.atomic():
        request = SrpRequest.objects.create(
            **base,
            sender_name=names.get(sender_id, ""),
            killmail_id=killmail_id, killmail_hash=killmail_hash, kill_time=killmail.killmail_time,
            victim_character_id=victim_character_id, victim_name=names.get(victim_character_id, ""),
            victim_corporation_id=victim_corporation_id,
            victim_corporation_name=names.get(victim_corporation_id, ""),
            ship_type_id=ship_type_id, ship_type_name=names.get(ship_type_id, ""),
            solar_system_id=killmail.solar_system_id,
            solar_system_name=names.get(killmail.solar_system_id, ""),
            ship_value=valuation["ship_value"], fit_value=valuation["fit_value"],
            cargo_value=valuation["cargo_value"], total_value=valuation["total_value"],
            unpriced_count=valuation["unpriced"], ship_price_source=valuation["ship_source"],
            priced_at=timezone.now(), validation_flags=flags)
        for row in rows:
            row.request = request
        SrpRequestItem.objects.bulk_create(rows)
    return request


def _validation_flags(*, sender_id, sender_name, victim_character_id, victim_name,
                      victim_corporation_id, kill_time, ship_type_id):
    """Soft warnings only -- a director decides; nothing here auto-denies."""
    from allianceauth.authentication.models import CharacterOwnership
    from allianceauth.eveonline.models import EveCorporationInfo

    flags = []
    if victim_character_id is None:
        flags.append("Victim is not a character")
    else:
        owners = {
            co.character.character_id: co.user_id
            for co in CharacterOwnership.objects.filter(
                character__character_id__in=[sender_id, victim_character_id]).select_related("character")
        }
        same_account = (
            owners.get(sender_id) is not None and owners.get(sender_id) == owners.get(victim_character_id))
        if sender_id != victim_character_id and not same_account:
            flags.append(f"Sender {sender_name} is not the victim {victim_name} (nor on the same Auth account)")
        if victim_character_id not in owners:
            flags.append("Victim character is not registered in Auth")
    if victim_corporation_id and not EveCorporationInfo.objects.filter(
            corporation_id=victim_corporation_id).exists():
        flags.append("Victim corporation is not a member corporation in Auth")
    max_age = getattr(settings, "YAGAS_SRP_MAX_KILL_AGE_DAYS", 14)
    if kill_time and kill_time < timezone.now() - timedelta(days=max_age):
        flags.append(f"Kill is older than {max_age} days")
    if ship_type_id in CAPSULE_TYPE_IDS:
        flags.append("Capsule loss (usually not covered)")
    return flags
