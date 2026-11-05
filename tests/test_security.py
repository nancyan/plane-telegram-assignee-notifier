import hashlib
import hmac

from app.security import verify_plane_signature


def test_verify_plane_signature():
    body = b'{"event":"workitem.created"}'
    secret = "plane_wh_test_secret"
    signature = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    assert verify_plane_signature(body, secret, signature)
    assert verify_plane_signature(body, secret, f"sha256={signature}")
    assert not verify_plane_signature(body + b" ", secret, signature)
    assert not verify_plane_signature(body, secret, None)
