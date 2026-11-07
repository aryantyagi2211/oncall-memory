import os

from dotenv import load_dotenv
from hindsight_client import Hindsight


load_dotenv()

HINDSIGHT_URL = os.getenv("HINDSIGHT_URL", "").strip()
HINDSIGHT_API_KEY = os.getenv("HINDSIGHT_API_KEY", "").strip()
BANK_ID = "oncall-v3"

INCIDENTS = [
    {
        "id": "INC-001",
        "date": "2026-05-08",
        "service": "payments-api / Redis",
        "error_log": "Checkout p99 3.8s; redis.exceptions.ConnectionError: Too many connections",
        "root_cause": "The payment API's connection pool was capped below peak checkout concurrency.",
        "fix": "Raised Redis max_connections from 50 to 200 and set the payments pool limit to 120.",
        "mttr": "22 minutes",
    },
    {
        "id": "INC-002",
        "date": "2026-05-19",
        "service": "auth-service",
        "error_log": "Login p95 4.2s; RedisTimeoutError on GET session:<id> after primary failover",
        "root_cause": "Auth workers kept using the old Redis primary address after failover.",
        "fix": "Reduced DNS cache TTL from 300s to 15s and restarted all 12 auth workers.",
        "mttr": "17 minutes",
    },
    {
        "id": "INC-003",
        "date": "2026-06-02",
        "service": "Kafka consumer / order-events",
        "error_log": "Consumer lag reached 1.2M; CommitFailedError: Offset commit cannot be completed",
        "root_cause": "Synchronous enrichment kept the consumer from polling within max.poll.interval.ms.",
        "fix": "Raised max.poll.interval.ms from 300000 to 900000 and moved enrichment to 8 workers.",
        "mttr": "41 minutes",
    },
    {
        "id": "INC-004",
        "date": "2026-06-14",
        "service": "payments-api / PostgreSQL",
        "error_log": "Checkout 500s; deadlock detected while updating payment_attempt and invoice",
        "root_cause": "Two transaction paths acquired payment_attempt and invoice locks in opposite order.",
        "fix": "Standardized lock order and reduced the transaction timeout from 30s to 8s.",
        "mttr": "36 minutes",
    },
    {
        "id": "INC-005",
        "date": "2026-06-27",
        "service": "catalog-api / Kubernetes",
        "error_log": "Pod terminated; Last State: OOMKilled, exit code 137; RSS peaked at 3.9Gi",
        "root_cause": "An unbounded in-memory product cache retained large payloads.",
        "fix": "Capped the cache at 20,000 entries and raised the container memory limit from 2Gi to 4Gi.",
        "mttr": "29 minutes",
    },
    {
        "id": "INC-006",
        "date": "2026-07-06",
        "service": "search-api / Redis",
        "error_log": "Redis CPU 100%; commandstats showed 42 KEYS calls/s and request timeouts",
        "root_cause": "A scheduled cleanup ran KEYS * against the production keyspace during peak traffic.",
        "fix": "Replaced KEYS with SCAN using COUNT 500 and limited cleanup to 2 batches/s.",
        "mttr": "13 minutes",
    },
    {
        "id": "INC-007",
        "date": "2026-07-18",
        "service": "notifications-consumer / Kafka",
        "error_log": "Group rebalance every 45s; heartbeat session timed out while processing batches",
        "root_cause": "The batch-size increase made email processing exceed the consumer session timeout.",
        "fix": "Reduced max.poll.records from 1000 to 200 and set the email worker pool to 16 threads.",
        "mttr": "22 minutes",
    },
    {
        "id": "INC-008",
        "date": "2026-07-29",
        "service": "profile-api / Kubernetes",
        "error_log": "Readiness probe failed with connection refused; rollout stalled at 40%",
        "root_cause": "Synchronous schema checks extended startup to 95s, but no startup probe was configured.",
        "fix": "Added a startup probe with failureThreshold 30 and periodSeconds 5; timeout set to 150s.",
        "mttr": "31 minutes",
    },
    {
        "id": "INC-009",
        "date": "2026-08-09",
        "service": "billing-worker / PostgreSQL",
        "error_log": "SQLSTATE 40001: could not serialize access due to concurrent update; 31% jobs failed",
        "root_cause": "12 workers updated the same monthly ledger rows under serializable isolation.",
        "fix": "Partitioned jobs by account across 24 queues and retried SQLSTATE 40001 up to 5 times with jitter.",
        "mttr": "48 minutes",
    },
    {
        "id": "INC-010",
        "date": "2026-08-21",
        "service": "auth-service",
        "error_log": "401 responses rose to 27%; JWT signature verification failed after key rotation",
        "root_cause": "The verifier's JWKS cache retained the previous signing key for 10 minutes.",
        "fix": "Reduced JWKS cache TTL from 600s to 30s and enabled refresh on unknown kid.",
        "mttr": "19 minutes",
    },
    {
        "id": "INC-011",
        "date": "2026-09-03",
        "service": "inventory-consumer / Kafka",
        "error_log": "Duplicate stock decrements; offset 883104 replayed after worker restart",
        "root_cause": "The consumer committed offsets before the inventory database transaction committed.",
        "fix": "Moved offset commits after DB commit, created a unique event_id index, and capped worker retries at 5 attempts.",
        "mttr": "54 minutes",
    },
    {
        "id": "INC-012",
        "date": "2026-09-15",
        "service": "uploads-api / Kubernetes",
        "error_log": "New pods in ImagePullBackOff for registry.internal/uploads:2026.09.15",
        "root_cause": "The deployment referenced an image tag before its registry upload had completed.",
        "fix": "Pinned the deployment to digest sha256:7f31a9c2 and added a 120s image-publish gate.",
        "mttr": "16 minutes",
    },
    {
        "id": "INC-013",
        "date": "2026-09-27",
        "service": "payments-api / Redis",
        "error_log": "Checkout errors; OOM command not allowed when used memory > maxmemory",
        "root_cause": "Payment quote cache keys had no TTL and Redis used the no-eviction policy.",
        "fix": "Set quote-key TTL to 900s and changed Redis maxmemory-policy to allkeys-lru.",
        "mttr": "27 minutes",
    },
    {
        "id": "INC-014",
        "date": "2026-10-12",
        "service": "reporting-api / PostgreSQL",
        "error_log": "DB CPU 94%; report_events queries scanned 86M rows for tenant/date filters",
        "root_cause": "A migration dropped the composite index on tenant_id and created_at.",
        "fix": "Recreated idx_report_events_tenant_created concurrently and set statement_timeout to 20s.",
        "mttr": "33 minutes",
    },
    {
        "id": "INC-015",
        "date": "2026-10-28",
        "service": "payments-api / Redis",
        "error_log": "RedisTimeoutError on GET payment:quote:<id>; checkout p99 reached 5.1s",
        "root_cause": "A failover left 80 payment API workers connected to the retired Redis primary.",
        "fix": "Reduced Redis DNS cache TTL from 120s to 10s and restarted the 80 stale workers.",
        "mttr": "24 minutes",
    },
]


def main() -> None:
    if not HINDSIGHT_URL:
        raise SystemExit("Set HINDSIGHT_URL in .env before seeding incidents.")

    if HINDSIGHT_API_KEY:
        client = Hindsight(base_url=HINDSIGHT_URL, api_key=HINDSIGHT_API_KEY)
    else:
        client = Hindsight(base_url=HINDSIGHT_URL)

    for index, incident in enumerate(INCIDENTS, start=1):
        content = (
            f"Incident ID: {incident['id']}\n"
            f"Date: {incident['date']}\n"
            f"Service: {incident['service']}\n"
            f"Error log: {incident['error_log']}\n"
            f"Root cause: {incident['root_cause']}\n"
            f"Exact fix: {incident['fix']}\n"
            f"MTTR: {incident['mttr']}"
        )
        client.retain(
            bank_id=BANK_ID,
            content=content,
            context="resolved production incident postmortem",
            timestamp=f"{incident['date']}T12:00:00Z",
            document_id=f"{BANK_ID}-{incident['id']}",
            metadata={
                "incident_id": incident["id"],
                "date": incident["date"],
                "service": incident["service"],
                "exact_fix": incident["fix"],
            },
        )
        print(f"Retained {incident['id']} ({index}/{len(INCIDENTS)})")


if __name__ == "__main__":
    main()
