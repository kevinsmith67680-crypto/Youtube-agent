# Security audit (Phase 8)

Date: 2026-11-14. Scope: the Python services (API, scheduler, worker, CLI), the Next.js
dashboard, the deployment artifacts and the repository history.

This audit records what was checked, how, and what was found. It is not a penetration test
and not a certification. Every "fixed" item has a test that would fail if it regressed.

## 1. Method

| Check | Tool / method | Result |
|---|---|---|
| Python dependency vulnerabilities | `pip-audit` on the 60 pinned packages | **0 known vulnerabilities** |
| Dashboard dependency vulnerabilities | `npm audit` (full tree) and `npm audit --omit=dev` | production dependencies: **0**; dev-only: 3 (see §3, H8) |
| Python static analysis | `bandit -r src` | 0 high and 0 medium severity after fixes (3 medium were false positives on fixed identifier lists, annotated `# nosec B608`); 99 low (asserts, `random` for jitter, subprocess with fixed argv) |
| Secrets in the tree and in history | `detect-secrets` over tracked files; `git log --all -p` searched for real key formats | no real credential found; 22 baselined fixture hits (`.secrets.baseline`) |
| Unicode "Trojan Source" | `bandit` B613 | 1 HIGH, fixed |
| Authentication and authorisation | code review + tests | see §2 |
| Container hardening | Dockerfile review + running the built images | see §4 |
| CI exposure | workflow review | see §5 |

CI repeats the dependency audits, `bandit` (medium and high fail the build) and the secret scan
against the baseline on every push and pull request.

## 2. Authentication, authorisation and input handling

| Surface | Control | Evidence |
|---|---|---|
| API reads | With `TREND_AGENT_API_TOKEN` set, every `/api/*` read needs the bearer token. Without it (local use) user-specific fields are removed. | `tests/unit/recommendations/test_api.py` |
| API private data and mutations | Need a configured token **and** the request must present it. Without a configured token they answer 401. | `test_api.py`; E2E `s26` |
| Generation (spends money) | Private, rate-limited separately (default 12/hour), capped per run (`MAX_RECOMMENDATIONS_PER_RUN`), refused (503) when the operating mode withdraws `recommendations`, and budget-guarded (LLM falls back to templates). | E2E `s11`, `s14`; `test_generation_is_refused_when_the_mode_withdraws_it` |
| Operational detail | `/metrics`, `/health/workers`, `/health/providers` and `/api/system/mode` need the token when one is configured. `/health/live` and `/health/ready` are public and expose nothing private. | E2E `s29`; `test_api_ops.py` |
| Dashboard | **New:** HTTP Basic authentication in Next.js middleware on every route except `/healthz`. It is mandatory in production: the dashboard answers 503 until credentials are configured, and the password must be at least 16 characters. Credentials are compared in constant time. The API token stays in the dashboard server's environment and is never sent to the browser. | `frontend/tests/auth.test.ts`; built image: 503 / 401 / 401 / 200 (§4) |
| Body size | 64 KiB limit on the API (413) and on the dashboard proxy | E2E `s30` |
| Rate limiting | Per client, with `Retry-After`; health probes are exempt | E2E `s31` |
| CORS | Only the configured origins; `*` is ignored | E2E `s32` |
| Security headers | `nosniff`, `DENY` framing, `no-referrer`, `no-store`, a restrictive CSP (API); the dashboard CSP allows no third-party origin | `test_api.py`; `next.config.mjs` |
| Correlation IDs | `X-Request-ID` is accepted only if it matches `^[A-Za-z0-9._:-]{8,64}$`; anything else is replaced, so log injection through the header is impossible | `test_api_ops.py` |

## 3. Findings and status

| ID | Severity | Finding | Status |
|---|---|---|---|
| H5 | HIGH | The dashboard had no authentication, yet acted with the server-held API token. Anyone who could reach it could read private data and trigger paid generation. | **Fixed**: middleware authentication, mandatory in production. |
| H4 | HIGH | Production accepted development defaults: the dev database password, a missing token, demo mode. | **Fixed**: `ops/production.py`. The API, scheduler and worker refuse to start in production (exit code 2) while an error remains. The built image refused the dev database URL with exit 2 (§4); E2E `s27`. |
| H7 | HIGH | Literal bidirectional control characters in a regex (`verification/untrusted.py`). | **Fixed**: replaced with `\u` escapes; equivalence checked over every code point. |
| H8 | HIGH (prod) / dev-only | `postcss` ≤ 8.5.22 in the production build chain, including the copy bundled in Next.js. `vitest` and `tinypool` are critical but dev-only. | **Production fixed**: `postcss` 8.5.32, forced into Next.js by an npm override (`npm audit --omit=dev` = 0). **Dev-only remaining**: vitest 3.2.7 (patched from 3.2.4). The fix is vitest 4.1.12 (major), whose installation fails with an npm arborist bug (`Cannot read properties of null (reading 'edgesOut')`). The affected code paths are the vitest UI server and mock registration, which are not used; tests run headless. Accepted risk; to be revisited when npm is fixed. |
| M2 | MEDIUM | No correlation IDs. | **Fixed**. |
| L1 | LOW | `bandit` low-severity items. | Reviewed, accepted: asserts for invariants; `random` used only for jitter and sampling; subprocess calls use fixed argv lists with no shell. |
| — | INFO | `detect-secrets` hits. | All fixtures: the local compose password, fake test keys, environment variable *names*, hashes. Baselined; new hits fail CI. |

## 4. Deployment artifacts (verified by running them)

* **Backend image**: multi-stage, non-root (uid 10001). Only the virtualenv, the code, the
  migrations and the configuration are copied; no compilers in the runtime layer; base images
  are pinned by digest. In the sandbox it was built with `INSTALL_PG_CLIENT=0`, because the
  sandbox blocks `deb.debian.org`; the default build installs `postgresql-client` for backups.
  Running it:
  * `migrate` applied revision 0009 to an empty database;
  * the API answered `/health/live` 200 and `/health/ready` 200, and `/health/workers` without a token 401;
  * `scheduler --once` and `worker --once` ran;
  * with `ENVIRONMENT=production` and the dev database URL, it exited with code 2 and named every error.
* **Frontend image**: multi-stage, `NODE_ENV=production`, non-root (uid 1000). Only production
  dependencies are in the runtime layer. Running it:
  * without credentials configured: 503, and `/healthz` 200;
  * with credentials configured: anonymous 401, wrong password 401, correct password 200.
* **Build-time CA**: an extra CA can be supplied for TLS-inspecting build proxies as a BuildKit
  secret (`build_ca`). It is never stored in a layer.
* **Production compose**: read-only root filesystems, `cap_drop: [ALL]`,
  `no-new-privileges`, loopback-only published ports (for a TLS-terminating reverse proxy) and
  resource limits. Every secret is a required variable with no default, and
  `docker compose config` refuses to render without them.

## 5. CI and supply chain

* The workflow runs with `permissions: contents: read`, uses `pull_request` (not
  `pull_request_target`), checks out with `persist-credentials: false`, and uses **no secrets**.
  Pull-request code can therefore never reach a deployment credential, and the workflow has
  no deployment job.
* Live-provider tests (`pytest -m live`) never run in CI.
* Dependencies are pinned: the Python lock via `uv pip freeze` evidence; npm via
  `package-lock.json` with exact versions; base images by digest.

## 6. Untrusted content and LLM safety (unchanged from Phases 5–7, re-verified by the suite)

External titles, descriptions, comments, feeds and community text are delimited as untrusted
data, scanned for injection signals and never executed. LLM output passes through the
verification gate. Nothing an LLM produces is turned into shell commands, SQL or
configuration changes. The Phase 8 tuning loop is statistical, and its output is a
proposal that a human must apply. Generated titles and hooks are filtered for instruction
text, credential-disclosure verbs and shell commands (`UNSAFE_TEXT`).

## 7. Residual risks and operator actions

1. Rotate the API token, dashboard password and signing key through the secret store, not
   by editing images. Use separate least-privilege database roles in production: an
   application role that cannot `DROP`, and a migration role used only by `migrate`.
2. Put the API and dashboard behind a TLS-terminating reverse proxy; never publish 8000/3000
   directly.
3. Restrict the YouTube API key to the YouTube Data API and, where possible, to server IPs.
4. Upgrade vitest to 4.x when the npm installer bug is fixed (dev-only exposure).
5. A penetration test and a dependency-update policy (for example, a monthly audit) are
   recommended before an internet-facing deployment.
