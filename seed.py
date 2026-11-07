import os

from dotenv import load_dotenv
from hindsight_client import Hindsight


load_dotenv()

HINDSIGHT_URL = os.getenv("HINDSIGHT_URL", "").strip()
HINDSIGHT_API_KEY = os.getenv("HINDSIGHT_API_KEY", "").strip()

INCIDENTS = [
    """Service: payments-api
Alert/logs: HTTP 500 rate reached 18%; psycopg2.errors.TooManyConnections: remaining connection slots are reserved.
Root cause: A new request path opened a PostgreSQL connection per request without returning connections to the pool.
Fix: Reverted the release, restored the connection pool, and added pool exhaustion alerts.
MTTR: 24 minutes.""",
    """Service: auth-service
Alert/logs: Login p95 exceeded 4s; RedisTimeoutError on GET session:<id>.
Root cause: A Redis failover left clients pinned to the old primary endpoint.
Fix: Refreshed service discovery, restarted auth workers, and added reconnect/backoff handling.
MTTR: 17 minutes.""",
    """Service: Kafka consumer / order-events
Alert/logs: Consumer lag grew to 1.2M; CommitFailedError: Offset commit cannot be completed.
Root cause: Slow synchronous enrichment exceeded the consumer max.poll.interval.ms.
Fix: Moved enrichment off the poll loop and increased partition-level worker capacity.
MTTR: 41 minutes.""",
    """Service: payments-api / PostgreSQL
Alert/logs: Checkout requests stalled; deadlock detected while updating payment_attempt and invoice rows.
Root cause: Two transaction paths locked the same tables in opposite order.
Fix: Standardized lock ordering, shortened the transaction, and retried SQLSTATE 40P01 safely.
MTTR: 36 minutes.""",
    """Service: catalog-api / Kubernetes
Alert/logs: Pod repeatedly restarted; Last State: Terminated, Reason: OOMKilled, exit code 137.
Root cause: An unbounded in-memory cache retained large product payloads.
Fix: Capped cache size, deployed the memory limit adjustment, and added heap metrics.
MTTR: 29 minutes.""",
    """Service: search-api / Redis
Alert/logs: Redis CPU at 100%; commandstats showed repeated KEYS * calls and request timeouts.
Root cause: A cleanup task used KEYS against the production keyspace during peak traffic.
Fix: Stopped the task and replaced KEYS with incremental SCAN batches.
MTTR: 13 minutes.""",
    """Service: notifications-consumer
Alert/logs: Kafka group rebalance every few seconds; heartbeat session timed out.
Root cause: A recent batch-size increase made message processing exceed the session timeout.
Fix: Reduced batch size, moved email delivery to a bounded worker pool, and monitored lag.
MTTR: 22 minutes.""",
    """Service: profile-api / Kubernetes
Alert/logs: Readiness probe failed with connection refused; rollout stalled at 40%.
Root cause: The application started slowly after adding synchronous schema checks, but the startup probe was missing.
Fix: Added a startup probe and moved the schema check into initialization with a longer timeout.
MTTR: 31 minutes.""",
    """Service: billing-worker / PostgreSQL
Alert/logs: Jobs failed with could not serialize access due to concurrent update (SQLSTATE 40001).
Root cause: Multiple workers updated the same monthly ledger rows under serializable isolation.
Fix: Partitioned jobs by account and retried serialization failures with bounded jitter.
MTTR: 48 minutes.""",
    """Service: auth-service
Alert/logs: 401 responses spiked immediately after key rotation; JWT signature verification failed.
Root cause: The verifier cache kept the previous signing key after the issuer rotated keys.
Fix: Invalidated the JWKS cache, enabled key-id-based refresh, and verified overlap during rotation.
MTTR: 19 minutes.""",
    """Service: inventory-consumer / Kafka
Alert/logs: Duplicate stock decrements reported; same event offset was processed repeatedly after restart.
Root cause: The consumer committed offsets before the database transaction committed.
Fix: Switched to commit-after-processing and made stock updates idempotent by event ID.
MTTR: 54 minutes.""",
    """Service: uploads-api / Kubernetes
Alert/logs: New pods failed startup; ImagePullBackOff for registry.internal/uploads:2025.04.12.
Root cause: The deployment referenced an image tag that had not completed publishing.
Fix: Rolled back to the last available immutable digest and gated rollout on image availability.
MTTR: 16 minutes.""",
    """Service: payments-api / Redis
Alert/logs: Checkout latency increased; Redis reported OOM command not allowed when used memory > maxmemory.
Root cause: Cache keys had no TTL and the instance used the no-eviction policy.
Fix: Added TTLs to payment quote keys and selected an eviction policy appropriate for the cache.
MTTR: 27 minutes.""",
    """Service: reporting-api / PostgreSQL
Alert/logs: Database CPU and query latency spiked; EXPLAIN showed sequential scan on report_events.
Root cause: A migration dropped the composite index used by tenant_id and created_at filters.
Fix: Recreated the index concurrently and added a migration regression check.
MTTR: 33 minutes.""",
    """Service: edge-gateway / Kubernetes
Alert/logs: 502s during scale-out; upstream connect error or disconnect/reset before headers.
Root cause: New pods became ready before the application had opened its upstream connection pools.
Fix: Tightened readiness checks, added graceful connection warm-up, and adjusted rollout surge settings.
MTTR: 26 minutes.""",
]


def main() -> None:
    if not HINDSIGHT_URL:
        raise SystemExit("Set HINDSIGHT_URL in .env before seeding incidents.")

    if HINDSIGHT_API_KEY:
        client = Hindsight(base_url=HINDSIGHT_URL, api_key=HINDSIGHT_API_KEY)
    else:
        client = Hindsight(base_url=HINDSIGHT_URL)

    for index, incident in enumerate(INCIDENTS, start=1):
        client.retain(bank_id="oncall", content=incident)
        print(f"Retained incident {index}/{len(INCIDENTS)}")


if __name__ == "__main__":
    main()
