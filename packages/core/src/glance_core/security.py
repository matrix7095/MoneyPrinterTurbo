import hashlib
import hmac
import secrets


def hash_secret(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def verify_secret(value: str, expected_hash: str) -> bool:
    return hmac.compare_digest(hash_secret(value), expected_hash)


def issue_token() -> str:
    return secrets.token_urlsafe(32)


def issue_role_token(root_secret: str, role_id: str) -> str:
    signature = hmac.new(
        root_secret.encode("utf-8"),
        f"glance-agent-role:{role_id}".encode(),
        hashlib.sha256,
    ).hexdigest()
    return f"glance-role.{role_id}.{signature}"


def verify_role_token(root_secret: str, token: str, role_ids: set[str]) -> str | None:
    parts = token.split(".", 2)
    if len(parts) != 3 or parts[0] != "glance-role" or parts[1] not in role_ids:
        return None
    expected = issue_role_token(root_secret, parts[1])
    return parts[1] if hmac.compare_digest(token, expected) else None


def issue_scheduler_token(root_secret: str, scheduler_id: str) -> str:
    signature = hmac.new(
        root_secret.encode("utf-8"),
        f"glance-agent-scheduler:{scheduler_id}".encode(),
        hashlib.sha256,
    ).hexdigest()
    return f"glance-scheduler.{scheduler_id}.{signature}"


def verify_scheduler_token(root_secret: str, token: str, scheduler_id: str) -> bool:
    return hmac.compare_digest(token, issue_scheduler_token(root_secret, scheduler_id))
