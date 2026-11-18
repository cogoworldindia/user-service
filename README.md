# user-service

Owns user records and profiles. The `id` is the user id minted by auth-service.

## API (public through the gateway; every route needs `Authorization: Bearer <access token>`)

| Method | Path | Notes |
|---|---|---|
| GET | `/v1/users/me` | The caller's full record and profile. Creates the user row on first call |
| PATCH | `/v1/users/me/profile` | Partial update: omitted fields are untouched and `null` clears a field. `status` can't be set |
| GET | `/v1/users/{id}` | **Public projection only** (`id, full_name, bio, profile_photo_url`). 404 if the user isn't active |

There is no route that takes a target user id for writes. A user can only ever modify their own
data, because identity comes from the verified token `sub`, never from the client.

## Events

Consumes `user.registered` from queue `user-service.user.registered` (quorum queue with a DLQ):
- **Idempotent**: the event id is recorded in `processed_events` in the same transaction as the
  write.
- A transient failure is retried with backoff and then nacked. The queue dead-letters the
  message after 5 deliveries.
- A malformed payload is rejected straight to the DLQ.

## Develop

```bash
uv sync
uv run pytest      # testcontainers: Postgres + RabbitMQ (needs Docker)
uv run ruff check . && uv run mypy app
DATABASE_URL=... uv run alembic upgrade head
```
