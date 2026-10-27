"""Shared log budgets; the role retains ownership of the Redis client."""
from .redis_log import RedisConversationLog


def create_realtime_log(client, *, namespace, policy):
    return RedisConversationLog(client, namespace=namespace,
        retention_ms=policy.retention_seconds * 1000,
        max_bytes=policy.max_log_bytes, ttl_seconds=policy.ttl_seconds)
