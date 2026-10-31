# Extension contract — Firebase hybrid satellite (FB-00 ADR, FB-A1, FB-R1/FS-R1, FB-N1/FS-I1, FB-H1, FB-B1)

Status: **local build, cloud not activated.** Every component below runs on this machine with mock adapters behind ports; no Firebase project, Firestore database, FCM key,
Hosting site or bucket was created or touched. The core product (FastAPI, workers, PostgreSQL) never depends on a satellite: with every mode `off` nothing changes.
Sample configuration: `config/firebase_hybrid.sample.yaml` (public, credential-free). Tests: `tests/final_loop/test_fb_identity_exchange.py`, `test_fb_projection.py`,
`test_fb_notifications.py`.

## 1. Architecture decision (FB-00)

| Decision | Choice in this build | Why |
|---|---|---|
| Identity provider | External provider (Firebase Auth / enterprise IdP) is an identity provider only; **SQL memberships are the authorization authority** | no authority duplication; revoke is a SQL fact, never a cached claim |
| Session model | the exchange issues the **existing** Ripple session (same `ripple_v2` HttpOnly Secure SameSite=strict cookie, same server-side revocation and lifetime rules) | one session model, one logout, one place to revoke |
| Account mapping | **invitation/admin mapping only**: a person with `membership.manage` links (issuer, project, identity tenant, subject) to one account; no auto-join from e-mail, domain or claim | a provider account is not a company membership |
| Realtime transport | **backend projector -> minimal opaque hints** in a sink (Firestore when activated; memory sink locally); the browser re-fetches the authorized API; bounded polling stays the fallback | hints carry no business data, so the revoke window only concerns "something changed" |
| Notifications | durable **intent** derived from the committed event; recipients resolved at dispatch time from current memberships; generic payload + opaque key; inbox is the full path; FCM is an opt-in hint | best-effort UX, never authority; works when push is refused |
| Hosting / storage | not changed: documented compatibility and blockers (sections 6, 7) | no deploy or bucket action is authorized |
| One transport | Firestore hints **or** nothing; no Realtime Database alongside | one cost/security model |

Identity trust map: token -> verifier (port) -> issuer/audience/tenant/expiry/freshness checks -> **active link** -> enabled account -> session. Claims are never read for roles.
Capability matrix: every protected operation re-reads SQL permissions per request (unchanged Final behaviour); the satellite adds no capability.
Per-component disable: each mode `off` returns 503 on its mutating routes and the rest of the product is unaffected (tests prove `off`).

## 2. FB-A1 identity exchange

Routes: `GET /v2/auth/firebase/config` (public: mode, project id; no credential), `POST /v2/auth/firebase/exchange {id_token}` (no session yet; origin check), `POST /v2/auth/external/links`,
`POST /v2/auth/external/links/revoke`, `GET /v2/auth/external/links` (`membership.manage`). Table `v2_external_identities` (migration `ext_t06`, identity schema).
Verifier port: `MockVerifier` (only in `RIPPLE_FIREBASE_MODE=local-mock`; token = `mock:` + claims JSON), `AdminSdkVerifier` (`firebase_admin.auth.verify_id_token(check_revoked=True)`,
lazy import; missing library or project id -> 503, **never** a fallback to the mock). Checks: issuer `https://securetoken.google.com/<project>` and audience = the one configured project;
optional identity tenant; expiry; `auth_time` within `RIPPLE_FIREBASE_MAX_AUTH_AGE_SECONDS` (fresh sign-in for the exchange); link active; account enabled. Every failure is one generic
401 `sign-in refused`; the reason code is in the audit (`external_identity.denied`), never in the response; subjects appear hashed in lists and audit; no token is stored or logged.
Collision: a subject linked to another account is refused until that link is revoked. Revoking a link stops future exchanges and touches neither sessions nor memberships (revoke
those with the existing routes). Not in this build: Microsoft/SAML specifics, custom-claim hints, account recovery policy, browser sign-in page.

## 3. FB-R1 / FS-R1 realtime status projection

`infra/adapters/v2_projection.py`: `project_once(store, sink, tenant, workspace)` reads committed rows past a durable checkpoint (`v2_projection_checkpoints`, migration `ext_t07`):
outbox rows (job queued, knowledge published, change completed) and generated documents; writes one hint per row `{schema ripple-hint/1, key, family, revision, changed_at, expires_at}`
where `key` is a hash (never a job id, artifact id, rule text or amount), `family` is one of `work | knowledge | change | document | other`, `revision` is monotonic per scope; the sink's
put is **conditional monotonic** (an older revision is ignored). The checkpoint advances only after the sink accepted the batch: an outage advances nothing and the next pass replays
the same rows (idempotent). Scope = one feed per workspace (`ws/<hash>`); per-recipient partitions are a later profile. Routes: `GET /v2/projection/status` (lag: unprojected outbox
rows and documents), `POST /v2/projection/run` (`membership.manage`; sink `off` -> 503). The projector never creates jobs, documents or provider calls. Not in this build: a real
Firestore database, listener client, latency measurement, recipient-level feeds.

Firestore sink, emulator only (`infra/adapters/v2_firestore_sink.py`, `RIPPLE_PROJECTION_SINK=firestore-emulator`): the same port over the Firestore REST API with the conditional
monotonic put (read, compare revision, commit with an `updateTime` / `exists:false` precondition). It writes only to a loopback `FIRESTORE_EMULATOR_HOST` with a `demo-` project
(`RIPPLE_FIRESTORE_PROJECT`); any other host or project is refused (route 503 `sink_refused`), an unreachable emulator is `sink_unavailable` — never a cloud fallback; no credential is
read. Layout `feeds/{feed_id}/items/{key}` (`feed_id` = scope with `/` -> `~`). Rules `config/firestore.rules`: a client reads a feed only when its ID token lists the feed id in the
custom claim `ripple_feeds`; no client write; everything else denied (the analyst conversation profile is a separate feed). `GET /v2/projection/feeds?workspace_id=`
(`project.read`) returns `{schema ripple-feeds/1, claim, feeds, token_issued: false}`: the workspace hint feed and the redacted conversation feed for every member, the analyst feed only
with `knowledge.confirm` — the claim value a token issuer must use. Run the emulator tests (Java 21 and firebase-tools needed), from a scratch working directory (the CLI writes
debug logs into its working directory):
`firebase emulators:start --only firestore --project demo-ripple-test --config <repo>/config/firebase.emulator.json` (loopback 127.0.0.1:8085, UI off), then
`FIRESTORE_EMULATOR_HOST=127.0.0.1:8085 RIPPLE_FIRESTORE_PROJECT=demo-ripple-test python -m pytest tests/final_loop/test_fs_emulator.py -q` with the test database variables, then stop
the emulator. Without the emulator variables the emulator cases are skipped and the refusal cases still run.

Listener tokens (`infra/adapters/v2_feed_tokens.py`): `POST /v2/projection/feed-token {workspace_id}` (`project.read`, origin-checked) returns
`{schema ripple-feed-token/1, token, token_kind, expires_at, feeds, policy_version}`. Every claim is decided by the server — `ripple_feeds` (= `feed_grants` over the member's
current permissions), `ripple_ws` (workspace feed id), `ripple_policy` (workspace membership policy version), `ripple_until_ms` (issue + 15 min), subject `rpl-<hash>` (never the
account id); a body with any field besides `workspace_id` is 400. The rules accept a read only for a listed feed, before `ripple_until_ms`, and while
`feed_policies/{ripple_ws}.policy_version` equals `ripple_policy`; every membership grant/revoke bumps the version and the backend mirrors it (after the change, on each projection
run, on each issue), so a revoked member's token stops at once and the others ask for a new token. Issuer `RIPPLE_FEED_TOKEN_ISSUER=emulator` only (unsigned token the local
emulator accepts; requires the emulator sink) — `issuer_off` 503 otherwise; minting a real Firebase custom token needs the owner's project and a server credential (not built).

Listener client (frontend; contract for the UI, not built in the backend): get a token, sign in to Firebase with it (custom token in a real deployment), listen to
`feeds/{feed_id}/items` for each id in `feeds`; on any hint re-fetch the authorized API (never render hint content); on `permission-denied` or at `expires_at` ask for a new token
(a 401/403 from the token route means the membership is gone: stop listening); keep bounded polling of the API as the fallback when the listener is unavailable.

## 4. FB-N1 / FS-I1 notifications and inbox

`infra/adapters/v2_notifications.py` (migration `ext_t08`): `derive_intents` turns every confirmed change request (`v2_business_change_intents`, the durable committed event) into one
`PENDING` intent (idempotent by change id). `dispatch_once(store, transport, ...)`: candidates = current members whose roles include `change.approve`; a candidate is a recipient only
when the review view says the knowledge decision is open **for them** (the requester/confirmer is excluded by the distinct-approver policy); a change nobody can still decide
(decided, committed, knowledge moved) makes the intent `STALE` with no sends. Every recipient gets an inbox item (`v2_inbox_items`, idempotent) — the full path when push is refused;
every registered valid token (`v2_notification_tokens`: stored for routing, **never returned by any API, never logged**) gets one delivery (`v2_notification_deliveries`, idempotent per
intent+token) through the transport port with at most 3 attempts; an unregistered token is retired. Payload: `{title, body, key, schema}` — generic text and an opaque inbox key; no
amounts, rule text, approval token or job id. Routes: `POST /v2/notifications/tokens`, `POST .../tokens/revoke`, `GET /v2/notifications/inbox` (items with `open | resolved | not_yours`
state computed against the current review view, unread count), `POST .../inbox/{item_id}/read` (idempotent receipt; decides nothing), `POST /v2/notifications/dispatch`
(`membership.manage`; transport `off` -> 503). Not in this build: browser service worker, FCM adapter, quiet hours/digests, department feeds, native mobile.

## 5. FS-C1 / FS-O1

FS-C1 (conversation read model, local build): `project_conversations(store, sink, …, profile)` with profiles `redacted` and `analyst`, each in its own scope and checkpoint: one bounded document per interview turn (state; the analyst profile adds the server's question text, the reading status and the questioner, never the person's answer or a prompt) and one per multi-agent run (status; the analyst profile adds counts and the report hash, never a finding statement). Route `POST /v2/projection/conversations` (`membership.manage`; the analyst profile needs `knowledge.confirm`). Canonical rows stay in SQL; the projection is disposable and rebuilt from the checkpoint. OPEN: Firestore rules, the access-profile decision, retention.

FS-O1 (offline reference, local build, **disabled by default**): `POST /v2/business/documents/{artifact_id}/offline-manifest` with `{workspace_id, consent: true}` (SEC-05, 2026-10-30: recording the consent writes an audit row, so it is a POST with the origin check and a human actor; a GET of the same path answers 405 and records nothing) answers 503 unless the deployment runs with `RIPPLE_OFFLINE_REFERENCE=trusted-device`, 409 without the explicit consent flag, and otherwise a labelled manifest of ONE available document (version, content hash, size, download time, expiry after `RIPPLE_OFFLINE_REFERENCE_DAYS` (7), `current_authority_checked: false`, purge-on list, rules: reading only, no approval/decision/apply offline, purge on logout/account/workspace switch, reconnect re-checks); audited `OFFLINE_REFERENCE_MANIFEST`. The bytes still come from the content route under the base document's permission. OPEN: the organisation's device/data policy, real browser persistence evidence.

## 6. FB-H1 hosting compatibility (configuration validated, not deployed)

`tools/firebase/validate_hosting.py` checks a Hosting configuration (sample `config/firebase.hosting.sample.json`) before any deploy: no API rewrite until `ripple.session_strategy` is decided and `allow_api_rewrite` is set, SPA fallback must exclude `/v2/**`, `index.html` no-store, private API never publicly cacheable, a safe public directory, dotfiles ignored.

Firebase Hosting rewrites to a backend forward only the `__session` cookie; the product's `ripple_v2` cookie would not be forwarded, so an `/api` rewrite is **blocked** until a
same-origin session strategy is decided (rename with migration and security review, or a separate API origin with a CORS/CSRF contract). Hosting may serve the static `app-next`
bundle only; private API and artifact responses stay `no-store`. No `firebase deploy` is part of this repository.

## 7. FB-B1 storage compatibility (adapter contract tested, bucket not touched)

`tests/final_loop/test_fb_hosting_storage.py` proves the adapter contract on a fake bucket: digest-addressed keys under the prefix, idempotent retry, content/digest mismatch refused, tampered bytes behind the precondition refused, digest and prefix validation.

The existing immutable blob adapter (`infra/adapters/v2_blobs.py`, content-addressed keys, generation precondition, SQL metadata/ACL gate) works with any bucket the server identity may
write; importing the bucket into Firebase is not required and signed URLs are not used (they outlive revocation). Verification on a real bucket needs an authorized disposable bucket
and is OPEN.

## 8. Not claimed

Any real provider sign-in, Firestore delivery, FCM delivery, hosting or bucket behaviour; latency; cost. Emulator/mock evidence proves pipeline shape and guards only.
