"""
Synthetic-but-realistic incident history for Northwind Payments, a fictional
mid-size payments platform (approx. 400 RPS peak, 9 services, EKS + Aurora
Postgres + MSK + ElastiCache).

The dataset is deliberately structured so that failure modes *repeat* across
months with different responders and slightly different symptoms. That is what
gives Hindsight something real to consolidate: after enough incidents, the
agent stops treating each alert as novel and starts recognising families.

Ordered oldest -> newest.
"""

from __future__ import annotations

INCIDENTS: list[dict] = [
    {
        "incident_id": "INC-1042",
        "opened_at": "2025-04-11T02:14:00Z",
        "service": "payments-api",
        "severity": "SEV2",
        "responder": "priya.raghavan",
        "failure_class": "db-connection-pool-exhaustion",
        "alert_title": "payments-api p99 latency > 4s, checkout error rate 6.1%",
        "alert_payload": (
            "ALERT: PaymentsApiLatencyHigh\n"
            "p99=4180ms (threshold 800ms) for 9m\n"
            "HTTP 503 rate 6.1% on POST /v1/charges\n"
            "log: HikariPool-1 - Connection is not available, request timed out after 30000ms\n"
            "log: org.postgresql.util.PSQLException: FATAL: sorry, too many clients already"
        ),
        "symptoms": "Latency cliff rather than ramp. CPU on pods flat at 30%. Aurora writer CPU 41%, connections pinned at max_connections=200.",
        "false_leads": "First 22 minutes were spent chasing a Kubernetes node upgrade that had landed the same evening. It was unrelated.",
        "root_cause": "A new /v1/charges/retry endpoint opened its own connection per request without returning it to the Hikari pool. Under retry storms the pool drained and every other endpoint starved behind it.",
        "fix": "Roll back the retry endpoint, then re-deploy with the shared pool bean and maxLifetime=25m; raise Aurora max_connections only as a temporary buffer.",
        "verification": "p99 back under 700ms within 6 minutes of rollback; pool active connections dropped from 200 to 34.",
        "mttr_minutes": 74,
        "customer_impact": "~11,400 failed charge attempts, $0 confirmed revenue loss after retries.",
        "tags": ["latency", "postgres", "connection-pool", "rollback"],
    },
    {
        "incident_id": "INC-1055",
        "opened_at": "2025-05-03T13:41:00Z",
        "service": "ledger-worker",
        "severity": "SEV2",
        "responder": "marcus.oyelaran",
        "failure_class": "kafka-consumer-lag",
        "alert_title": "ledger-worker consumer lag 1.2M messages on topic billing.settlements",
        "alert_payload": (
            "ALERT: KafkaConsumerLagCritical\n"
            "group=ledger-worker topic=billing.settlements lag=1,214,880 (threshold 50,000)\n"
            "log: Member ledger-worker-7c9 sending LeaveGroup request due to consumer poll timeout\n"
            "log: Attempt to heartbeat failed since group is rebalancing"
        ),
        "symptoms": "Lag climbing linearly for 40 minutes. Settlement reports stale. No errors in the worker logs beyond rebalance chatter.",
        "false_leads": "Team initially scaled the consumer group from 6 to 18 pods, which made the rebalance storm worse and added 15 minutes.",
        "root_cause": "max.poll.interval.ms was left at the 5-minute default while a new FX-conversion call inside the handler occasionally took 6+ seconds per message. Slow batches tripped the poll timeout, triggering continuous rebalances that prevented any progress.",
        "fix": "Raise max.poll.interval.ms to 900000, cut max.poll.records from 500 to 50, and move the FX lookup behind a 200ms-timeout cache. Do NOT scale the consumer group during a rebalance storm.",
        "verification": "Lag drained from 1.2M to under 5k in 38 minutes with zero rebalances.",
        "mttr_minutes": 96,
        "customer_impact": "Settlement reporting delayed 2h for 340 merchants. No funds movement affected.",
        "tags": ["kafka", "consumer-lag", "rebalance", "throughput"],
    },
    {
        "incident_id": "INC-1061",
        "opened_at": "2025-05-19T08:02:00Z",
        "service": "auth-gateway",
        "severity": "SEV1",
        "responder": "dana.whitfield",
        "failure_class": "jwt-key-rotation-failure",
        "alert_title": "auth-gateway 401 rate 100% for merchant API tokens",
        "alert_payload": (
            "ALERT: AuthGateway401Spike\n"
            "401 rate = 99.8% on /oauth/introspect for 4m\n"
            "log: JWSVerificationException: no matching key found for kid=nw-2025-05-a\n"
            "log: JWKS fetch from https://keys.internal.northwind/.well-known/jwks.json returned 200, keys=1"
        ),
        "symptoms": "Total merchant API outage. Dashboard logins (session cookie path) unaffected, which masked the blast radius for the first few minutes.",
        "false_leads": "Suspected a bad deploy of auth-gateway; the deploy had actually happened 6 days earlier.",
        "root_cause": "Key rotation published the new signing key to the JWKS endpoint but the gateway caches JWKS for 24h and had no negative-cache invalidation. Tokens signed with the new kid could not be verified until the cache expired.",
        "fix": "Force JWKS refresh via the admin endpoint (POST /admin/jwks/refresh), then keep both old and new keys published for a full 48h overlap window on every future rotation.",
        "verification": "401 rate fell to baseline 0.3% within 90 seconds of the forced refresh.",
        "mttr_minutes": 41,
        "customer_impact": "Full merchant API outage for 38 minutes. 61 merchants raised tickets.",
        "tags": ["auth", "jwt", "key-rotation", "cache"],
    },
    {
        "incident_id": "INC-1067",
        "opened_at": "2025-06-02T17:26:00Z",
        "service": "checkout-web",
        "severity": "SEV2",
        "responder": "priya.raghavan",
        "failure_class": "cdn-cache-poisoning",
        "alert_title": "checkout-web serving stale asset bundle to ~30% of sessions",
        "alert_payload": (
            "ALERT: CheckoutJsErrorRate\n"
            "window.onerror rate 22/min (threshold 2/min)\n"
            "browser: Uncaught TypeError: t.initCardField is not a function\n"
            "cdn: X-Cache: HIT, Age: 41203, ETag mismatch on /static/checkout.8f2a.js"
        ),
        "symptoms": "Errors only for users on two POPs. Hard refresh fixed it for individual users, which made reproduction unreliable.",
        "false_leads": "Half an hour lost trying to reproduce locally; the local build was always fresh so the bug never appeared.",
        "root_cause": "The deploy pipeline uploaded the new bundle but the CDN purge step silently failed (403 from the purge API after a token rotation), leaving two POPs serving an 11-hour-old bundle against a new HTML shell.",
        "fix": "Manually purge the affected paths, then make the pipeline fail loudly on a non-2xx purge response and add a post-deploy assertion that fetches the asset from three POPs and compares hashes.",
        "verification": "JS error rate returned to 1/min after purge; POP hash assertion added to CI.",
        "mttr_minutes": 88,
        "customer_impact": "Checkout card field failed to render for an estimated 4,900 sessions.",
        "tags": ["cdn", "deploy", "frontend", "cache"],
    },
    {
        "incident_id": "INC-1073",
        "opened_at": "2025-06-21T22:55:00Z",
        "service": "fraud-scorer",
        "severity": "SEV2",
        "responder": "marcus.oyelaran",
        "failure_class": "model-server-oom",
        "alert_title": "fraud-scorer pods in CrashLoopBackOff, scoring falls open",
        "alert_payload": (
            "ALERT: FraudScorerPodRestarts\n"
            "restarts=14 in 10m, reason=OOMKilled\n"
            "kubectl: State: Terminated  Reason: OOMKilled  Exit Code: 137\n"
            "log: torch allocated 3.81 GiB of 4.00 GiB limit before eviction"
        ),
        "symptoms": "Scoring service unavailable, so the payment path fell open (approve-by-default). Fraud loss risk, not availability risk.",
        "false_leads": "Initially blamed a traffic spike; RPS was actually 8% below the weekly median.",
        "root_cause": "A new model artifact shipped with batch_size=64 baked into the config while the container memory limit stayed at 4Gi. Peak allocation exceeded the limit on the first large batch.",
        "fix": "Pin batch_size=16 via env override and raise the memory limit to 8Gi, then add a pre-deploy memory smoke test that scores a synthetic 64-item batch before promotion.",
        "verification": "Zero restarts over the following 6 hours; peak RSS 5.2Gi.",
        "mttr_minutes": 63,
        "customer_impact": "47 minutes of fall-open scoring; 3 chargebacks later attributed to the window.",
        "tags": ["oom", "kubernetes", "ml", "memory-limit"],
    },
    {
        "incident_id": "INC-1080",
        "opened_at": "2025-07-08T04:33:00Z",
        "service": "pg-primary",
        "severity": "SEV1",
        "responder": "dana.whitfield",
        "failure_class": "replica-lag-failover",
        "alert_title": "Aurora replica lag 340s, read queries returning stale balances",
        "alert_payload": (
            "ALERT: AuroraReplicaLagHigh\n"
            "AuroraReplicaLag=341,900ms on nw-prod-reader-2 (threshold 5,000ms)\n"
            "log: canceling statement due to conflict with recovery\n"
            "log: vacuum of ledger_entries running 2h47m"
        ),
        "symptoms": "Merchant dashboards showed balances minutes out of date. Writes were healthy throughout.",
        "false_leads": "Considered an emergency failover; that would have been actively harmful with lag this high.",
        "root_cause": "A long-running manual VACUUM FULL on ledger_entries held a lock that blocked replay on the reader, compounded by a nightly analytics job doing a full table scan against the same reader.",
        "fix": "Kill the VACUUM FULL, route analytics to the dedicated reporting reader, and never run VACUUM FULL on ledger_entries outside the maintenance window. Do not fail over while replay lag is climbing.",
        "verification": "Lag drained to under 800ms in 12 minutes after the vacuum was killed.",
        "mttr_minutes": 57,
        "customer_impact": "Stale dashboard balances for 52 minutes. No incorrect settlements.",
        "tags": ["postgres", "replica-lag", "vacuum", "aurora"],
    },
    {
        "incident_id": "INC-1085",
        "opened_at": "2025-07-27T00:07:00Z",
        "service": "billing-cron",
        "severity": "SEV3",
        "responder": "sam.okonkwo",
        "failure_class": "timezone-boundary-bug",
        "alert_title": "billing-cron produced duplicate invoices for 218 merchants",
        "alert_payload": (
            "ALERT: DuplicateInvoiceDetector\n"
            "duplicate_invoice_count=218 in cycle 2025-07\n"
            "log: cycle window computed as 2025-06-30T23:00Z..2025-07-31T23:00Z (expected 2025-07-01T00:00Z..2025-08-01T00:00Z)"
        ),
        "symptoms": "No alerting from the job itself; the duplicate detector caught it 7 hours later.",
        "false_leads": "None; the log line pointed straight at the window computation.",
        "root_cause": "The billing window was computed in Europe/London local time and then treated as UTC. During BST the one-hour offset pulled the previous cycle's last hour into the current cycle.",
        "fix": "Compute all billing windows in UTC explicitly, add a property test asserting window boundaries across DST transitions, and void + reissue the affected invoices before merchants are charged.",
        "verification": "Reissued 218 invoices; property test now covers both DST transitions.",
        "mttr_minutes": 210,
        "customer_impact": "218 duplicate invoices voided before collection. No merchant was double-charged.",
        "tags": ["billing", "timezone", "dst", "data-correctness"],
    },
    {
        "incident_id": "INC-1088",
        "opened_at": "2025-08-14T19:12:00Z",
        "service": "payments-api",
        "severity": "SEV2",
        "responder": "sam.okonkwo",
        "failure_class": "db-connection-pool-exhaustion",
        "alert_title": "payments-api 503s during flash sale, pool saturated",
        "alert_payload": (
            "ALERT: PaymentsApiErrorRate\n"
            "HTTP 503 rate 4.4% for 6m on POST /v1/charges\n"
            "log: HikariPool-1 - Connection is not available, request timed out after 30000ms\n"
            "metric: hikaricp_connections_pending = 187"
        ),
        "symptoms": "Same signature as INC-1042 but triggered by legitimate traffic, not a bad endpoint. Aurora writer CPU 68%.",
        "false_leads": "Team almost rolled back a deploy from that afternoon. The deploy was innocent.",
        "root_cause": "Pool size (30 per pod x 8 pods = 240) exceeded what Aurora could serve alongside the reporting workload during a 3.2x traffic spike. The pool was correctly configured for normal load and undersized for the sale.",
        "fix": "Enable PgBouncer in transaction pooling mode in front of the writer and cap per-pod pool at 15; pre-scale PgBouncer before announced sales.",
        "verification": "503 rate to 0.2% within 9 minutes of PgBouncer cutover; pending connections dropped to 0.",
        "mttr_minutes": 52,
        "customer_impact": "~6,100 failed charges during a flash sale window.",
        "tags": ["latency", "postgres", "connection-pool", "pgbouncer", "traffic-spike"],
    },
    {
        "incident_id": "INC-1094",
        "opened_at": "2025-08-30T11:48:00Z",
        "service": "notification-svc",
        "severity": "SEV3",
        "responder": "priya.raghavan",
        "failure_class": "provider-rate-limit",
        "alert_title": "notification-svc receipt emails delayed 45m, 429s from provider",
        "alert_payload": (
            "ALERT: NotificationQueueDepth\n"
            "queue_depth=84,000 (threshold 5,000)\n"
            "log: provider responded 429 Too Many Requests, Retry-After: 60\n"
            "log: retry attempt 7/7 exhausted for message batch b-99213"
        ),
        "symptoms": "Queue depth climbing, no errors visible to merchants beyond delayed receipts.",
        "false_leads": "Scaling senders up doubled the 429 rate and made the backlog worse.",
        "root_cause": "A marketing blast shared the same provider account and consumed the shared 10k/min quota, starving transactional receipts. Retries used a fixed 60s delay with no jitter, producing a thundering herd every minute.",
        "fix": "Move transactional mail to a dedicated provider subaccount with its own quota, add exponential backoff with jitter, and give transactional messages a strict priority lane over marketing.",
        "verification": "Backlog drained in 26 minutes after the subaccount cutover; 429 rate to zero.",
        "mttr_minutes": 71,
        "customer_impact": "84k receipt emails delayed by up to 45 minutes.",
        "tags": ["rate-limit", "queue", "email", "backoff"],
    },
    {
        "incident_id": "INC-1099",
        "opened_at": "2025-09-15T15:20:00Z",
        "service": "redis-sessions",
        "severity": "SEV2",
        "responder": "marcus.oyelaran",
        "failure_class": "cache-eviction-storm",
        "alert_title": "redis-sessions evicting 40k keys/min, users logged out mid-checkout",
        "alert_payload": (
            "ALERT: RedisEvictedKeys\n"
            "evicted_keys rate = 41,200/min (threshold 500/min)\n"
            "info: used_memory_human=13.9G maxmemory=14G maxmemory-policy=allkeys-lru\n"
            "log: checkout-web session lookup miss rate 38%"
        ),
        "symptoms": "Users bounced to login during checkout. Redis CPU normal, memory pinned at the ceiling.",
        "false_leads": "Suspected a session-service bug; the service was behaving correctly against a cache that kept dropping its keys.",
        "root_cause": "A new 'recently viewed products' feature wrote unbounded lists into the same Redis cluster with no TTL, pushing memory to maxmemory. allkeys-lru then evicted session keys indiscriminately.",
        "fix": "Set a 30-minute TTL on the recently-viewed keys and move them to a separate cache; change the session cluster policy to volatile-lru so keys without a TTL can never be evicted.",
        "verification": "Eviction rate to 0 within 4 minutes of the TTL backfill; session miss rate to 0.4%.",
        "mttr_minutes": 66,
        "customer_impact": "Estimated 2,300 checkout sessions interrupted.",
        "tags": ["redis", "eviction", "ttl", "sessions"],
    },
    {
        "incident_id": "INC-1104",
        "opened_at": "2025-09-29T06:55:00Z",
        "service": "ledger-worker",
        "severity": "SEV2",
        "responder": "dana.whitfield",
        "failure_class": "kafka-consumer-lag",
        "alert_title": "ledger-worker lag 480k after MSK broker replacement",
        "alert_payload": (
            "ALERT: KafkaConsumerLagCritical\n"
            "group=ledger-worker topic=billing.settlements lag=487,340\n"
            "log: Group coordinator is unavailable or invalid, will attempt rediscovery\n"
            "log: Offset commit failed: The coordinator is not aware of this member"
        ),
        "symptoms": "Lag spiked immediately after AWS replaced an unhealthy MSK broker.",
        "false_leads": "Restarted the consumer group twice, which reset coordination and cost about 12 minutes.",
        "root_cause": "Broker replacement moved the group coordinator; the consumer's session.timeout.ms of 10s was too tight for the rediscovery window, so members kept getting fenced.",
        "fix": "Raise session.timeout.ms to 45000 and heartbeat.interval.ms to 15000, then let the group settle without restarting it. Same rule as INC-1055: do not restart or scale during a rebalance.",
        "verification": "Group stabilised in 7 minutes once left alone; lag drained in 31 minutes.",
        "mttr_minutes": 58,
        "customer_impact": "Settlement lag of ~50 minutes. No data loss.",
        "tags": ["kafka", "consumer-lag", "msk", "rebalance"],
    },
    {
        "incident_id": "INC-1112",
        "opened_at": "2025-10-12T14:09:00Z",
        "service": "payments-api",
        "severity": "SEV1",
        "responder": "sam.okonkwo",
        "failure_class": "bad-feature-flag-rollout",
        "alert_title": "payments-api 500s on 100% of 3DS challenge flows",
        "alert_payload": (
            "ALERT: PaymentsApi5xx\n"
            "HTTP 500 rate 100% on POST /v1/charges/3ds/challenge\n"
            "log: NullPointerException at ThreeDsChallengeHandler.buildAcsRequest(ThreeDsChallengeHandler.java:114)\n"
            "flag: three_ds_v2_flow rolled 0% -> 100% at 14:07Z"
        ),
        "symptoms": "Failure began exactly two minutes after a flag change. Only the 3DS path affected.",
        "false_leads": "None. The flag audit log made the cause obvious in under 3 minutes.",
        "root_cause": "The three_ds_v2_flow flag was flipped straight from 0% to 100% without a canary. The v2 path required an acsTransId that legacy merchant configs did not populate.",
        "fix": "Kill the flag back to 0% immediately, then require staged rollouts (1% -> 10% -> 50% -> 100%) with a 15-minute soak and automatic rollback on 5xx delta for any payment-path flag.",
        "verification": "Error rate to baseline 22 seconds after the flag was reverted.",
        "mttr_minutes": 11,
        "customer_impact": "~800 3DS challenges failed over 9 minutes.",
        "tags": ["feature-flag", "rollout", "3ds", "fast-rollback"],
    },
    {
        "incident_id": "INC-1118",
        "opened_at": "2025-10-26T09:31:00Z",
        "service": "auth-gateway",
        "severity": "SEV2",
        "responder": "priya.raghavan",
        "failure_class": "jwt-key-rotation-failure",
        "alert_title": "auth-gateway intermittent 401s, ~18% of introspect calls",
        "alert_payload": (
            "ALERT: AuthGateway401Spike\n"
            "401 rate = 18.2% on /oauth/introspect\n"
            "log: JWSVerificationException: no matching key found for kid=nw-2025-10-b\n"
            "log: 3 of 14 gateway pods report jwks_cache_age_seconds > 80000"
        ),
        "symptoms": "Partial, not total, unlike INC-1061. Failures tracked exactly to three pods.",
        "false_leads": "Load balancer stickiness was investigated first because the failures looked user-specific.",
        "root_cause": "Same class as INC-1061: JWKS cache staleness after rotation. This time only the three pods that had not been recycled since before the rotation held the stale cache, because the 48h overlap window from INC-1061 had been quietly reduced to 12h.",
        "fix": "Force JWKS refresh on all pods, restore the 48h dual-key overlap, and add a jwks_cache_age_seconds alert at 6h so staleness is caught before tokens fail.",
        "verification": "401 rate to 0.3% within 2 minutes of the forced refresh across all pods.",
        "mttr_minutes": 29,
        "customer_impact": "Intermittent auth failures for 24 minutes affecting roughly 1 in 6 API calls.",
        "tags": ["auth", "jwt", "key-rotation", "cache", "partial-outage"],
    },
    {
        "incident_id": "INC-1121",
        "opened_at": "2025-11-04T16:44:00Z",
        "service": "checkout-web",
        "severity": "SEV3",
        "responder": "marcus.oyelaran",
        "failure_class": "frontend-performance-regression",
        "alert_title": "checkout-web LCP regressed from 1.9s to 4.6s on mobile",
        "alert_payload": (
            "ALERT: CoreWebVitalsRegression\n"
            "LCP p75 mobile = 4,610ms (was 1,900ms)\n"
            "bundle: main.js 412KB -> 1.31MB gzip\n"
            "build: added dependency moment-timezone@0.5.43 + full locale set"
        ),
        "symptoms": "No errors, no alerts from the service itself. Conversion rate on mobile dropped 3.1% before anyone noticed.",
        "false_leads": "Blamed on a CDN edge change; the CDN was fine.",
        "root_cause": "A date-formatting change pulled in moment-timezone with all locales and all IANA data, tripling the main bundle.",
        "fix": "Replace with the native Intl.DateTimeFormat API, add a CI budget that fails the build if main.js gzip exceeds 500KB.",
        "verification": "Bundle back to 398KB; LCP p75 back to 1.85s the next day.",
        "mttr_minutes": 320,
        "customer_impact": "Estimated 3.1% mobile conversion drop over ~2 days.",
        "tags": ["frontend", "performance", "bundle-size", "conversion"],
    },
    {
        "incident_id": "INC-1126",
        "opened_at": "2025-11-18T23:02:00Z",
        "service": "fraud-scorer",
        "severity": "SEV2",
        "responder": "dana.whitfield",
        "failure_class": "model-server-oom",
        "alert_title": "fraud-scorer OOMKilled again after model v4 promotion",
        "alert_payload": (
            "ALERT: FraudScorerPodRestarts\n"
            "restarts=9 in 8m, reason=OOMKilled\n"
            "kubectl: Reason: OOMKilled  Exit Code: 137\n"
            "log: loading model artifact fraud-v4 (1.9GB) alongside fraud-v3 during warm swap"
        ),
        "symptoms": "Restarts started 4 minutes after the v4 promotion. Same OOMKilled signature as INC-1073.",
        "false_leads": "None; the team recognised the signature from INC-1073 within 5 minutes.",
        "root_cause": "The warm-swap deploy holds both the old and new model in memory simultaneously. With v4 at 1.9GB the transient peak exceeded the 8Gi limit set after INC-1073.",
        "fix": "Switch to cold-swap (drain pod, unload, load) for artifacts over 1.5GB and size memory limits at 2.5x the artifact size rather than a flat number.",
        "verification": "Clean promotion with zero restarts on retry; peak RSS 6.1Gi.",
        "mttr_minutes": 34,
        "customer_impact": "21 minutes of degraded scoring capacity, no fall-open (circuit breaker added after INC-1073 held).",
        "tags": ["oom", "kubernetes", "ml", "deploy", "repeat-incident"],
    },
    {
        "incident_id": "INC-1131",
        "opened_at": "2025-12-01T20:38:00Z",
        "service": "payments-api",
        "severity": "SEV2",
        "responder": "priya.raghavan",
        "failure_class": "db-connection-pool-exhaustion",
        "alert_title": "payments-api pool saturation on Cyber Monday, PgBouncer client_wait 9s",
        "alert_payload": (
            "ALERT: PaymentsApiLatencyHigh\n"
            "p99=3,120ms, HTTP 503 rate 2.9%\n"
            "pgbouncer: cl_waiting=412 avg_wait_time=9,180,000us pool_mode=transaction\n"
            "log: HikariPool-1 - Connection is not available, request timed out after 30000ms"
        ),
        "symptoms": "Third occurrence of this family. PgBouncer from INC-1088 was in place but itself became the bottleneck.",
        "false_leads": "Briefly suspected Aurora; writer CPU was only 44%.",
        "root_cause": "PgBouncer default_pool_size was left at 20 server connections per user/db pair, so it queued clients rather than passing them through. The fix from INC-1088 was applied but never load-tested at peak.",
        "fix": "Raise default_pool_size to 80 and max_client_conn to 5000, run PgBouncer as 4 replicas behind an NLB, and load-test the pool path at 3x peak before every announced sale.",
        "verification": "cl_waiting to 0 and p99 to 610ms within 5 minutes of the config reload.",
        "mttr_minutes": 43,
        "customer_impact": "~2,700 slow or failed charges during peak Cyber Monday hour.",
        "tags": ["latency", "postgres", "connection-pool", "pgbouncer", "traffic-spike", "repeat-incident"],
    },
    {
        "incident_id": "INC-1137",
        "opened_at": "2025-12-19T03:17:00Z",
        "service": "pg-primary",
        "severity": "SEV2",
        "responder": "sam.okonkwo",
        "failure_class": "replica-lag-failover",
        "alert_title": "Aurora reader lag 190s during index build",
        "alert_payload": (
            "ALERT: AuroraReplicaLagHigh\n"
            "AuroraReplicaLag=192,400ms on nw-prod-reader-1\n"
            "log: CREATE INDEX CONCURRENTLY idx_ledger_merchant_created running 51m\n"
            "log: canceling statement due to conflict with recovery"
        ),
        "symptoms": "Same family as INC-1080. Reads stale, writes fine.",
        "false_leads": "None; the runbook note from INC-1080 was found in under 4 minutes.",
        "root_cause": "A CREATE INDEX CONCURRENTLY on a 900M-row table generated replay volume the reader could not keep up with during peak hours.",
        "fix": "Pause the index build, resume it inside the 02:00-05:00 UTC maintenance window with max_parallel_maintenance_workers reduced, and hold the INC-1080 rule: never fail over while replay lag is climbing.",
        "verification": "Lag under 1s within 9 minutes of pausing the build.",
        "mttr_minutes": 26,
        "customer_impact": "Stale dashboard reads for 22 minutes.",
        "tags": ["postgres", "replica-lag", "index", "aurora", "repeat-incident"],
    },
    {
        "incident_id": "INC-1142",
        "opened_at": "2026-01-09T10:12:00Z",
        "service": "notification-svc",
        "severity": "SEV3",
        "responder": "marcus.oyelaran",
        "failure_class": "provider-rate-limit",
        "alert_title": "notification-svc SMS 429s during OTP surge",
        "alert_payload": (
            "ALERT: NotificationQueueDepth\n"
            "queue_depth=19,400 channel=sms\n"
            "log: provider responded 429 Too Many Requests, Retry-After: 30\n"
            "log: otp send p95 latency 41s (threshold 5s)"
        ),
        "symptoms": "OTP delivery slow enough that users abandoned login. Email channel healthy.",
        "false_leads": "None; the email lesson from INC-1094 pointed straight at quota separation.",
        "root_cause": "SMS shared a single provider quota across OTP and marketing reminders. Same shape as INC-1094 but on a channel where the fix had never been applied.",
        "fix": "Split OTP onto a dedicated SMS subaccount with reserved throughput and apply the jittered exponential backoff that was added for email in INC-1094.",
        "verification": "OTP p95 back to 3.2s within 18 minutes; queue drained fully in 25.",
        "mttr_minutes": 47,
        "customer_impact": "Roughly 6,400 OTPs delayed; login abandonment up 12% for 40 minutes.",
        "tags": ["rate-limit", "sms", "otp", "backoff", "repeat-incident"],
    },
    {
        "incident_id": "INC-1149",
        "opened_at": "2026-02-02T18:24:00Z",
        "service": "ledger-worker",
        "severity": "SEV2",
        "responder": "dana.whitfield",
        "failure_class": "kafka-consumer-lag",
        "alert_title": "ledger-worker lag 760k after settlement schema change",
        "alert_payload": (
            "ALERT: KafkaConsumerLagCritical\n"
            "group=ledger-worker topic=billing.settlements lag=761,220\n"
            "log: SerializationException: Unknown magic byte!\n"
            "log: consumer poll returned 0 records for 14 consecutive polls"
        ),
        "symptoms": "Lag climbing but, unlike INC-1055 and INC-1104, no rebalance churn at all.",
        "false_leads": "Team applied the INC-1055 poll-timeout fix first. It did nothing, costing 14 minutes — the signature looked similar but the cause was different.",
        "root_cause": "A producer began writing Avro with a new schema ID that was not registered in the consumer's schema registry cache, so every message failed deserialization and the consumer made no progress.",
        "fix": "Register the schema version, restart consumers to refresh the registry cache, and add a compatibility gate in CI that blocks producer schema changes without a registered backwards-compatible version.",
        "verification": "Lag drained in 44 minutes after schema registration.",
        "mttr_minutes": 79,
        "customer_impact": "Settlement processing delayed 1h20m for all merchants.",
        "tags": ["kafka", "consumer-lag", "avro", "schema-registry"],
    },
    {
        "incident_id": "INC-1153",
        "opened_at": "2026-03-06T12:50:00Z",
        "service": "redis-sessions",
        "severity": "SEV3",
        "responder": "priya.raghavan",
        "failure_class": "cache-eviction-storm",
        "alert_title": "redis-sessions eviction rate 9k/min after cart-hold feature launch",
        "alert_payload": (
            "ALERT: RedisEvictedKeys\n"
            "evicted_keys rate = 9,140/min\n"
            "info: used_memory_human=13.6G maxmemory=14G maxmemory-policy=volatile-lru\n"
            "log: cart_hold:* keys written with no TTL, count=1.1M"
        ),
        "symptoms": "Sessions survived this time (volatile-lru change from INC-1099 held), but cart holds were being dropped.",
        "false_leads": "None; the INC-1099 postmortem was the first thing the responder opened.",
        "root_cause": "Same family as INC-1099: a new feature wrote unbounded keys without TTL. The volatile-lru policy protected sessions, so the blast radius was contained to the new feature itself.",
        "fix": "Apply a 45-minute TTL to cart_hold keys and add a pre-launch checklist item requiring a TTL and a memory estimate for any new Redis key pattern.",
        "verification": "Eviction rate to 120/min after TTL backfill; memory to 9.8G.",
        "mttr_minutes": 31,
        "customer_impact": "Approximately 900 cart holds dropped early.",
        "tags": ["redis", "eviction", "ttl", "repeat-incident"],
    },
    {
        "incident_id": "INC-1158",
        "opened_at": "2026-04-14T07:39:00Z",
        "service": "ledger-worker",
        "severity": "SEV2",
        "responder": "sam.okonkwo",
        "failure_class": "poison-message-loop",
        "alert_title": "ledger-worker stuck reprocessing one settlement batch for 3 hours",
        "alert_payload": (
            "ALERT: LedgerWorkerThroughputZero\n"
            "processed_messages_total flat for 181m, lag stable at 12,400\n"
            "log: ArithmeticException: / by zero at SettlementSplitter.split(SettlementSplitter.java:88)\n"
            "log: message offset 44,201,933 retried 5,412 times"
        ),
        "symptoms": "Lag was NOT growing, which is why the standard lag alert never fired. Throughput was simply zero.",
        "false_leads": "None, but detection took 3 hours because no alert covered flat throughput with stable lag.",
        "root_cause": "A settlement batch with zero line items caused a divide-by-zero in the splitter. With infinite retries and no dead-letter queue, the consumer retried the same offset forever.",
        "fix": "Add a dead-letter topic after 5 attempts, guard the zero-item case, and add a throughput-flatline alert that fires independently of consumer lag.",
        "verification": "Poison message dead-lettered; throughput resumed immediately; backlog cleared in 20 minutes.",
        "mttr_minutes": 194,
        "customer_impact": "Settlements delayed 3h20m for 1,100 merchants.",
        "tags": ["kafka", "poison-message", "dlq", "alerting-gap"],
    },
]


#: Alerts held out of the seeded history, used to demo the agent live.
DEMO_ALERTS: list[dict] = [
    {
        "label": "Repeat family — payments-api pool saturation",
        "expect": "Should recognise the INC-1042 / INC-1088 / INC-1131 family and lead with PgBouncer pool sizing.",
        "text": (
            "ALERT: PaymentsApiLatencyHigh\n"
            "service=payments-api env=prod\n"
            "p99=3,640ms (threshold 800ms) sustained 7m\n"
            "HTTP 503 rate 3.4% on POST /v1/charges\n"
            "log: HikariPool-1 - Connection is not available, request timed out after 30000ms\n"
            "pgbouncer: cl_waiting=298 avg_wait_time=7,400,000us\n"
            "aurora writer CPU 46%, no deploys in the last 6 hours"
        ),
    },
    {
        "label": "Repeat family — ledger-worker lag with no rebalance",
        "expect": "Should surface INC-1149 (schema) over INC-1055 (poll timeout) because there is no rebalance churn.",
        "text": (
            "ALERT: KafkaConsumerLagCritical\n"
            "group=ledger-worker topic=billing.settlements lag=402,880\n"
            "log: SerializationException: Unknown magic byte!\n"
            "no rebalance events in the last 30 minutes\n"
            "consumer poll returning 0 records"
        ),
    },
    {
        "label": "Repeat family — auth 401s after rotation",
        "expect": "Should surface INC-1061 and INC-1118 and lead with the forced JWKS refresh.",
        "text": (
            "ALERT: AuthGateway401Spike\n"
            "401 rate = 24% on /oauth/introspect\n"
            "log: JWSVerificationException: no matching key found for kid=nw-2026-08-a\n"
            "signing key rotated 40 minutes ago"
        ),
    },
    {
        "label": "Novel — no matching history",
        "expect": "Should honestly report that memory holds nothing similar rather than pattern-matching something irrelevant.",
        "text": (
            "ALERT: WebhookDeliverySuccessRateLow\n"
            "service=webhook-dispatcher env=prod\n"
            "delivery success rate 61% (threshold 99%)\n"
            "log: x509: certificate signed by unknown authority for merchant endpoint\n"
            "started 12 minutes ago, affects 40% of merchant endpoints"
        ),
    },
]
