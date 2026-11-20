# user-service

Owns user records and profiles, personality tags, cover photos, media uploads and eKYC. The `id`
is the user id minted by auth-service. The full API, with examples and error codes, is in
`contracts/openapi/user-service.json` and the workbook `docs/api/CoGo-API-Reference.xlsx`
(integration repo).

## API (public through the gateway)

Every route except the KYC webhook needs `Authorization: Bearer <access token>`. Identity
always comes from the verified token, so a user can only ever change their own data.

| Method | Path | Notes |
|---|---|---|
| GET, PATCH | `/v1/me` | Own profile, stats, `kyc_level`, `kyc_prompt`, `identity_locked`, `guide_seen`. PATCH bumps `version` and emits `user.updated` |
| PUT | `/v1/me/cover-photos` | Up to 3 |
| POST | `/v1/me/guide/seen` | First-run coach marks |
| GET | `/v1/personality-tags` | |
| POST | `/v1/media/upload-url`, `/v1/media/{id}/confirm` | Presigned S3 PUT, then confirm |
| GET | `/v1/users/{id}` | Public profile (age, never DOB or KYC detail); 404 `profile_unavailable` if blocked |
| GET | `/v1/me/kyc` | Status of each check; the app polls it after the ID flow |
| POST | `/v1/me/kyc/prompt/snooze` | "Skip for now" (`kyc.prompt_cooldown_days`) |
| POST | `/v1/me/kyc/email/start`, `/v1/me/kyc/email/verify` | Official-email OTP |
| POST | `/v1/me/kyc/gov-id/start` | `{consent: true}` → `{attempt_id, redirect_url}` |
| POST | `/v1/webhooks/kyc/{provider}` | Provider callback: no token, the body signature is checked |
| GET, POST | `/v1/kyc/mock/{ref}` | Development only: the mock provider's page |
| GET, PATCH | `/v1/users/me`, `/v1/users/me/profile` | Deprecated app-v0 aliases |

## eKYC (`app/kyc/`)

- `service.py`: the checks, the level (`cogo_common.kyc.derive_kyc_level`) and the prompt.
- `providers.py`: the `KycProvider` interface and the mock.
- `cashfree.py`: Cashfree Secure ID (DigiLocker).
- `names.py`: name matching.

Selected by `KYC_PROVIDER` (`mock` | `cashfree`). Misconfiguration fails at startup, and the mock
is refused in staging and production. Only outcomes are stored: the verified email, ID type,
last 4 digits and match booleans. The name and DOB on the ID are never stored, logged or queued.

## Events

Publishes through the transactional outbox:
- `user.created` and `user.updated`: fat snapshots with `version`.
- `user.kyc_level_changed`: the same snapshot plus `previous_kyc_level`.

Official-email codes go to `notification.email.requested` by a direct publish, never via the
outbox.

Consumes on queue `user-service` (quorum, with a DLQ; idempotent through `processed_events`):
- `user.registered`
- `social.followed` / `social.unfollowed` (counters)
- `journey.created` / `journey.deleted` (`journeys_count`)
- `social.user_blocked` / `social.user_unblocked` (`block_projection`)

## Develop

```bash
uv sync --all-extras
uv run pytest      # testcontainers: Postgres, RabbitMQ, RustFS (needs Docker)
uv run ruff check . && uv run mypy app
DATABASE_URL=... uv run alembic upgrade head
```
