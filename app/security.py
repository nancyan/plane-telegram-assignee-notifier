import hashlib
import hmac


def verify_plane_signature(raw_body: bytes, secret: str, signature: str | None) -> bool:
    if not signature:
        return False
    if signature.startswith("sha256="):
        signature = signature.removeprefix("sha256=")
    expected = hmac.new(secret.encode(), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)


def hash_link_code(code: str) -> str:
    return hashlib.sha256(code.encode()).hexdigest()


def constant_time_equal(left: str | None, right: str) -> bool:
    return bool(left) and hmac.compare_digest(left, right)
