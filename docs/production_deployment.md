# Production deployment

> **Status: nothing has been deployed.** No deployment target and no cloud credentials were
> available. The artifacts below were built and run locally (see `security_audit.md` §4);
> the readiness item `production_deployment` stays **BLOCKED** until an operator deploys
> and records it.

## 1. Topology (single host, justified by the load test)

| Service | Image | Replicas | Notes |
|---|---|---|---|
| migrate | backend | one-shot | `alembic upgrade head`; forward-only; runs before everything else |
| api | backend | 1+ | stateless; behind a TLS reverse proxy; `/health/ready` gates traffic |
| scheduler | backend | 1 active (+ standby allowed) | the database lease guarantees a single owner |
| worker | backend | 2 (≤ `MAX_CONCURRENT_WORKERS`) | scale out freely; `SKIP LOCKED` leases |
| frontend | frontend | 1+ | HTTP Basic authentication is mandatory; holds the API token server-side |
| database | managed PostgreSQL 16 | 1 | daily backups + point-in-time recovery from the provider; plus `system backup` |

There is no Kubernetes, message broker or cache: PostgreSQL provides leases, queues and
`LISTEN/NOTIFY`. The single-host load test (`docs/benchmarks/phase8_load_*.json`) shows the
API is far from saturation at the expected single-creator load. Revisit the topology only
if measured load requires it.

## 2. Build

```
docker build -t REGISTRY/trend-agent-backend:VERSION .
docker build -t REGISTRY/trend-agent-frontend:VERSION frontend
```

* **Backup tools:** the backend image installs `postgresql-client` by default. Pass
  `--build-arg INSTALL_PG_CLIENT=0` only if backups run elsewhere.
* **Build-time CA:** behind a TLS-inspecting proxy, pass the CA as a BuildKit secret:
  `--secret id=build_ca,src=ca.crt`. It is never stored in a layer.
* **Base images:** pinned by digest. To update, pull, record the new digest and rebuild.

## 3. Configure

Create an env file **outside the repository** (for example `/etc/trend-agent/prod.env`, mode
600):

| Variable | Requirement |
|---|---|
| `BACKEND_IMAGE`, `FRONTEND_IMAGE` | pinned tags |
| `TREND_AGENT_DATABASE_URL` | application role (no DDL rights) and `sslmode=require` for a remote database |
| `TREND_AGENT_API_TOKEN` | ≥ 32 random characters |
| `TREND_AGENT_VERIFICATION_SIGNING_KEY` | ≥ 32 random characters |
| `TREND_AGENT_API_CORS_ORIGINS` | the dashboard's https origin |
| `DASHBOARD_BASIC_AUTH_USER`, `DASHBOARD_BASIC_AUTH_PASSWORD` | password ≥ 16 characters |
| `TREND_AGENT_YOUTUBE_API_KEY` | needed for proxy research with gap analysis; restrict the key in the Cloud Console |
| `TREND_AGENT_SMTP_*` or `TREND_AGENT_TELEGRAM_*` | so ops alerts reach a person |
| budgets: `TREND_AGENT_DAILY_API_BUDGET`, `TREND_AGENT_DAILY_LLM_BUDGET`, `TREND_AGENT_MONTHLY_LLM_BUDGET`, … | see `cost_optimization.md` |

The services enforce this. With `TREND_AGENT_ENVIRONMENT=production` the API, scheduler and
worker refuse to start (exit code 2) while any configuration error remains. Run
`trend-agent system audit --strict` against the env file first.

Use a separate migration role for `migrate` (`TREND_AGENT_DATABASE_URL` overridden on that
service), and an application role with only `SELECT/INSERT/UPDATE/DELETE` for the others.

## 4. Deploy

```
docker compose -f docker-compose.prod.yml --env-file /etc/trend-agent/prod.env pull
docker compose -f docker-compose.prod.yml --env-file /etc/trend-agent/prod.env up -d
docker compose -f docker-compose.prod.yml --env-file /etc/trend-agent/prod.env ps     # all healthy
```

Order is enforced: `migrate` must complete successfully before the API, scheduler and
worker start, and the frontend waits for a healthy API.

**Migrations.** They are forward-only and additive by policy. A destructive change needs a
separate, reviewed migration and a backup taken immediately before. Downgrades are never run
automatically; revision 0009's downgrade refuses while a model is `rejected`.

**Rollback.** Redeploy the previous image tags. A schema rollback is a manual,
backed-up procedure. Revision 0009 is additive (new tables, one widened check constraint),
but running the previous (0.7) images against a 0009 database was **not tested**. The safe
path is to take a backup immediately before upgrading, and restore it if the release must be
rolled back.

## 5. Verify after deploying

```
curl -fsS https://API_HOST/health/ready
trend-agent system health            # inside the api container
trend-agent system smoke-test        # one request per configured provider
trend-agent system readiness
```

Then record the deployment:

```sql
INSERT INTO system_state (key, value, updated_at)
VALUES ('production_deployment', '{"at": "...", "version": "...", "by": "..."}', now());
```

Do not record it unless it happened.

## 6. CI

`.github/workflows/ci.yml` runs on every push and pull request:

* lint;
* types (src strict, tests strict);
* unit tests (3.12 and 3.13);
* integration and E2E against PostgreSQL;
* migration validation on an empty database;
* security scans;
* frontend checks and build;
* docker builds with a non-root check and compose validation.

It has read-only permissions, no secrets and no deployment job: pull-request code can never
reach production credentials. If automated deployment is added later, put it in a separate
workflow, triggered only from protected branches or tags, using an environment with
required reviewers.

## 7. Local full stack

```
docker compose --profile app up -d --build      # development credentials only
```
