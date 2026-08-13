# micro_bank

A Transaction Ledger API: idempotent transfers between accounts, with money
conservation and concurrency correctness enforced at the database level, not
in application memory. Built on Django + Django REST Framework + PostgreSQL.

Scope grew beyond the original take-home spec (transfers, idempotency,
concurrency) into a fuller backend: JWT auth, a custom KYC-lite user model,
admin CRUD, soft delete, cursor pagination, Swagger/OpenAPI docs, and a
Django admin panel. The core hard requirements below are still the spine of
the design.

## Stack

- Django 6.1 + Django REST Framework
- PostgreSQL (via Docker)
- Redis (via Docker) -- caches completed/failed transfer responses for idempotent retries
- SimpleJWT (stateless auth)
- drf-spectacular (OpenAPI schema, Swagger UI, ReDoc)

## Local setup

```bash
# 1. Start Postgres + Redis
docker compose up -d

# 2. Python environment
python -m venv venv
source venv/bin/activate
pip install -r bank/requirements.txt

# 3. Configure environment
cp bank/.env.example bank/.env   # then fill in the values, see below

# 4. Migrate
cd bank
python manage.py migrate

# 5. Create a staff user (see gotcha below)
python manage.py createsuperuser

# 6. Run
python manage.py runserver
```

### Environment variables (`bank/.env`)

| Variable | Purpose |
|---|---|
| `DEBUG` | `True` for local dev. Set `False` for anything resembling production. |
| `SECRET_KEY` | Django's signing key -- also signs JWTs. Generate your own: `python -c "import secrets; print(secrets.token_urlsafe(50))"`. Never reuse a key that's ever been committed or shared. |
| `ALLOWED_HOSTS` | Comma-separated hostnames. Empty is fine while `DEBUG=True`; required once `DEBUG=False`. |
| `DATABASE_NAME`, `DATABASE_USER`, `DATABASE_PASSWORD`, `DATABASE_HOST`, `DATABASE_PORT` | Must match `docker-compose.yml`. |
| `REDIS_URL` | Cache backend for idempotent-replay lookups. If Redis is unreachable, requests just fall through to Postgres -- not a hard dependency. |

### `createsuperuser` gotcha

The user model requires a CNIC or passport number (a `CheckConstraint`, not
just app-level validation), but that field isn't in `REQUIRED_FIELDS` by
default in a way `createsuperuser` always surfaces cleanly on every Django
version -- if it errors asking for CNIC and doesn't prompt for it, create one
via shell instead:

```bash
python manage.py shell -c "
from users.models import User
User.objects.create_superuser(
    email='admin@example.com', password='<pick one>',
    phone_number='+10000000000', first_name='Admin', last_name='User',
    cnic='0000000000000',
)
"
```

### Running tests

```bash
python manage.py test --keepdb
```

Includes real-thread, real-connection concurrency tests
(`accounts.tests.ConcurrencyTests`) -- these need the actual Postgres
container running, not SQLite, since they exercise real row-level locking
and real transaction visibility, not something an in-memory test DB can
faithfully reproduce.

Use `--keepdb` rather than plain `python manage.py test`: the concurrency
tests open several real threaded connections, and an interrupted run can
leave old connections attached to `test_microbank`, which then blocks the
next run's attempt to drop and recreate that database (Postgres refuses to
drop a database with active sessions) and drops you into an interactive
`yes`/`no` prompt. `--keepdb` skips the drop/recreate step entirely and just
reuses the existing test database, sidestepping that whole failure mode --
and it's faster besides, since it doesn't replay every migration from
scratch on each run. If you ever do want a truly clean test database (e.g.
after changing a migration), drop it explicitly first:

```bash
docker compose exec db psql -U microbank -d microbank -c "DROP DATABASE IF EXISTS test_microbank;"
```

## API surface

Full interactive docs: `/api/docs/` (Swagger) and `/api/redoc/` (ReDoc) once
the server is running. Admin panel at `/admin/`.

| Endpoint | Notes |
|---|---|
| `POST /api/users/signup/` | Public. Enforces `AUTH_PASSWORD_VALIDATORS` and CNIC-or-passport. |
| `POST /api/users/login/`, `/login/refresh/` | JWT obtain/refresh. |
| `GET/PUT/PATCH/DELETE /api/users/me/` | Self-service. `DELETE` soft-deletes -- blocked if the user still holds any open account. |
| `GET/POST /api/users/` | Staff only. Lists all users (cursor-paginated); `POST` lets staff create a user directly. |
| `GET/PUT/PATCH/DELETE /api/users/<bank_user_id>/` | Staff only, full CRUD. |
| `GET/POST /api/accounts/` | Own accounts; staff see every account and can create one for another user via `owner_bank_user_id`. Requires `account_type`: `CURRENT` or `SAVINGS`, one of each per owner max (DB-enforced). |
| `GET /api/accounts/<account_number>/` | Owner or staff. Non-owner gets `404`, not `403` -- no account-number enumeration. |
| `GET /api/accounts/<account_number>/transactions/` | Ledger history (successful money movements only), cursor-paginated. |
| `GET /api/accounts/<account_number>/transfers/` | Every transfer attempt involving this account, including `FAILED` ones, cursor-paginated. |
| `POST /api/accounts/transfers/` | Requires `Idempotency-Key` header. See below. |
| `GET /api/accounts/transfers/<id>/` | Status lookup, restricted to the two accounts involved. |
| `POST /api/accounts/<account_number>/deposit/` | Superuser only, and only into their own account. Requires `Idempotency-Key`. This is the system's sole entry point for new money -- funding a customer account happens via a normal transfer from here, not another deposit call. |

## Design decisions

- **Balances are integer minor units** (cents), never floats, with a DB
  `CheckConstraint` enforcing `balance >= 0` -- so "never negative" can't be
  bypassed by a bug in application code, only by a DB-level constraint
  violation.
- **Double-entry ledger.** Every transfer writes two `Entry` rows (a debit
  and a credit) that sum to zero, instead of just mutating balances in
  place. This gives an audit trail for free and makes conservation
  independently checkable by summing entries.
- **Deposits are the only way money enters the system**, and only a
  superuser can deposit, only into their own account. Funding a customer
  happens via an ordinary transfer from that account. This keeps "money
  creation" to one narrow, auditable path instead of many.
- **Redis is a non-authoritative accelerator, not a dependency.** It caches
  idempotent-replay responses for speed; if it's down, every request just
  falls through to Postgres and gets the same correct (if slower) answer.

## How the hard requirements are met

**Idempotency.** `Idempotency-Key` is a DB `UNIQUE` constraint on `Transfer`
(and `Deposit`). The first request to use a key wins; a retry (or a losing
concurrent request) hits an `IntegrityError`, looks up the row that already
won, and replays its result instead of moving money again. Redis sits in
front of this as a fast path only -- a miss or a down cache still lands on
the same DB constraint and the same correct outcome.

**Conservation.** Debiting the source and crediting the destination happens
inside one atomic transaction -- both writes commit or neither does. The
`balance >= 0` constraint is enforced by Postgres itself, not app code, so
it holds even against a bug or a bypassed code path.

**Concurrency across multiple instances.** Both accounts in a transfer are
locked with `SELECT FOR UPDATE` (always in a fixed, id-ascending order, to
avoid deadlocking against a concurrent transfer in the opposite direction)
before either balance is read or changed. That lock -- and the idempotency
constraint above -- live in Postgres, the one thing every instance shares.
So it doesn't matter which stateless instance handles a given request, or
whether a request and its retry land on different instances: two instances
racing on the same account serialize at the DB row lock, not in memory, and
a retry racing its original request resolves at the DB constraint, not in
a process-local cache.

## What I left out

- **Rate limiting.** Would add per-user throttling on transfer/deposit
  before this went anywhere near production.
- **Multi-currency.** Everything assumes a single currency (USD); no
  conversion.
- **General audit log.** The ledger covers money movement, but there's no
  audit trail for other actions (e.g. an admin editing a user). Django
  admin's built-in log exists but isn't surfaced anywhere.
- **Account closure API.** `Account` has the soft-delete fields, but the
  only way to actually close one today is the Django admin action -- no
  public endpoint.

Given more time, account closure and rate limiting are what I'd build next
-- both are small, well-scoped additions on top of what's already there.
