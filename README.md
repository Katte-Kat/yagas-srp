# yagas-srp

Mail-driven ship replacement (SRP) inbox for [Alliance Auth](https://allianceauth.org/) 5.x.
Pilots mail a killmail link to one receiver character; directors and SRP managers see the requests
in Auth and approve or deny them.

Built for Baba Yagas and in production use there. Separate from `allianceauth.srp` on purpose.

## How it works

1. A manager picks the **receiver character** on `/srp-inbox/receivers/` (a character that already
   authorized `esi-mail.read_mail.v1`; managers see their own characters, superusers all) or
   authorizes a new one there.
2. `yagas_srp.tasks.poll_mailbox` (Celery beat, e.g. every 5 minutes, or the "Poll mailbox now"
   button) reads that character's inbox. Only mails addressed to it personally are processed.
3. A `killReport:ID:HASH` link, an ESI killmail URL or a zKillboard URL is turned into a request:
   the killmail is fetched from ESI (public endpoint) and valued.
4. Directors and SRP managers approve (with an editable payout), deny or reopen a request in
   `/srp-inbox/`.

Requests that cannot be turned into a valid request (no link, bad hash, duplicate killmail) are kept
as `invalid` so it is visible why a mail did not arrive in the queue.

## The inbox

- One tab per status: **Pending**, **Approved**, **Denied**, **Invalid** and **Archived** (old mails
  imported from the mailbox history, not up for a decision), each with its count and total value.
- Every column header sorts (`?sort=` and `?dir=`; numbers and dates start descending, text
  ascending, a second click flips); the sort is kept across tabs and pages.
- Decide straight from the list. Copy buttons hand out the pilot name, the payout in digits only and
  the payment reason `SRP #<number>` to paste into the wallet transfer.
- The menu entry carries a badge with the requests waiting for the viewer's decision.
- The regular poll reads the newest 200 mail headers and handles at most 25 new mails per run; the
  whole history is read once with `srp_backfill_mailbox` (below).

## Paid or not (optional)

When the yagas-finance-dashboard app is installed
and its SRP mark is set up, the inbox shows for approved requests whether the wallet booking that
paid them was found. The booking names the request in its reason (`SRP #123`). States: **paid**
(booked exactly the payout), **differs** (another amount), **shared** (one booking paid several
requests) and **open** (approved after the mark was set up, no booking yet). Without the finance app,
or when reading it fails, nothing is shown and the inbox works as before.

## Valuation

- Hull + fitted modules, rigs, drones and fighters count towards the payout; cargo, implants and
  holds are shown but not counted.
- Price = Jita 4-4 split, (highest buy + lowest sell) / 2, from Fuzzwork's market aggregates
  (cached one hour). Orders on one side only use that side; types without any Jita orders fall back
  to CCP's ESI average price. Both cases are flagged on the request.
- A Fuzzwork/ESI outage never silently switches the price basis: the mail is retried on the next
  poll. "Reprice" on a pending request re-values it live.
- All amounts are whole ISK.

## Tags

A request carries any number of **tags** (admin: *SRP tags*), for example `BLOPS SRP` and `SANCTION SRP`. Tags come from **tag rules** (admin: *SRP tag rules*), checked when a mail
becomes a request:

- `subject` rule: a whole word or phrase in the mail subject (`HUNT` matches "SRP hunt loss", not "hunters"); case does not matter.
- `body` rule: the same for the mail text.
- `hull` rule: the ship group of the loss from the static data, exactly (`Black Ops`).

A rule gives one or more tags. Rules only ever add tags, and a request whose tags somebody changed by hand (request page or admin) is left alone from then on. After changing rules, run
`python manage.py srp_tags` (`--dry-run` to look first, `--force` to include hand-edited requests) or use the admin action "Apply all active rules to every request now".

**Who sees what:** a tag can name **viewer groups** (Auth groups). A member of such a group opens the inbox (menu entry, list, request pages) but sees only the requests carrying a tag of
theirs, read-only: no approving, reopening, repricing or tag editing. Managers with `view_requests` still see everything and can filter by tag.

Migration 0007 seeds the Baba Yagas setup once: tags `BLOPS SRP` (viewer group `BLOPS SRP`, created empty) and `SANCTION SRP`; subject `HUNT` and `TARGET` give both tags, the hull
group `Black Ops` gives `BLOPS SRP`. Add the BLOPS people to the Auth group `BLOPS SRP`; change or delete the seeded rules in the admin as you like.

## Permissions

| Permission | Allows |
|---|---|
| `yagas_srp.view_requests` | see the inbox and every request |
| `yagas_srp.approve_requests` | approve / deny / reopen / reprice, and pull the mailbox |
| `yagas_srp.manage_receiver` | choose, activate, deactivate and remove receiver characters (also allows "Poll mailbox now") |
| `yagas_srp.assign_srp_managers` | hand out and take away the SRP Manager role on `/srp-inbox/managers/` |
| viewer group of a tag | see (read-only) the requests carrying that tag |

**SRP Manager role:** a plain Auth group (`SRP Manager`, created on demand) with `view_requests` and
`approve_requests`: it sees every request and decides on it, but cannot change the mail receiver or
hand out the role. Whoever holds `assign_srp_managers` adds a manager by typing a character name
(type-ahead over the registered characters) and removes one with one click.

## Install

```
pip install git+https://github.com/Katte-Kat/yagas-srp.git
```

```python
# local.py
INSTALLED_APPS += ["yagas_srp"]
CELERYBEAT_SCHEDULE["yagas_srp_poll_mailbox"] = {
    "task": "yagas_srp.tasks.poll_mailbox",
    "schedule": crontab(minute="*/5"),
}
```

Then `python manage.py migrate`, restart, and make sure the SSO application in the EVE developer
portal has the `esi-mail.read_mail.v1` scope enabled.

Optional setting: `YAGAS_SRP_MAX_KILL_AGE_DAYS` (default 14), kills older than that get a warning.

Contact address: the user agent sent to zKillboard and Fuzzwork carries `ESI_USER_CONTACT_EMAIL` from your Django settings (nothing is stored in this repository).

## Commands

- `python manage.py srp_backfill_mailbox [--since YYYY-MM-DD] [--archive-older-than-days N] [--dry-run] [--limit N]`
  reads the receiver's older mails once (newest first, paced to ESI's mail rate limit, so a big
  inbox takes an hour or more) and turns every personally addressed mail that links a killmail into
  a request; older ones become `archived`, younger ones stay pending. Mails without a link are
  skipped, not recorded as invalid. Safe to repeat.
- `python manage.py srp_tags [--dry-run] [--force]` applies the tag rules again (see Tags).
- `python manage.py srp_add_test_request --sender-id <character id> --body '<a href="killReport:ID:HASH">kill</a>'`
  runs one fake mail through the same pipeline for testing.

## License

MIT, see `LICENSE`.
