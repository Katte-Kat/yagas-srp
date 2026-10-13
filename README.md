# yagas-srp

Mail-driven ship replacement (SRP) inbox for [Alliance Auth](https://allianceauth.org/) 5.x.
Pilots mail a killmail link to one receiver character; directors see the requests in Auth and
approve or deny them.

Prototype, running on the Baba Yagas test instance. Not deployed on production.

## How it works

1. A manager picks the **receiver character** on `/srp-inbox/receivers/` (a character that already
   authorized `esi-mail.read_mail.v1`; managers see their own characters, superusers all) or
   authorizes a new one there.
2. `yagas_srp.tasks.poll_mailbox` (Celery beat, e.g. every 5 minutes, or the "Poll mailbox now"
   button) reads that character's inbox. Only mails addressed to it personally are processed.
3. A `killReport:ID:HASH` link, an ESI killmail URL or a zKillboard URL is turned into a request:
   the killmail is fetched from ESI (public endpoint) and valued.
4. Directors approve (with an editable payout), deny or reopen a request in `/srp-inbox/`.

Requests that cannot be turned into a valid request (no link, bad hash, duplicate killmail) are kept
as `invalid` so it is visible why a mail did not arrive in the queue.

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
| `yagas_srp.manage_receiver` | choose, activate, deactivate and remove receiver characters |
| viewer group of a tag | see (read-only) the requests carrying that tag |

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

`python manage.py srp_add_test_request --sender-id <character id> --body '<a href="killReport:ID:HASH">kill</a>'`
runs one fake mail through the same pipeline for testing.
