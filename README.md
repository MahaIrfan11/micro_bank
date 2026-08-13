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
