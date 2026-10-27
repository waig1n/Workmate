"""Small authenticated envelope shared by Worker and the shipped gateway script."""
import base64
import json
import secrets

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

MAX_REMOTE_BODY = 20 * 1024 * 1024
MAX_REMOTE_ENVELOPE = 40 * 1024 * 1024


def encrypt_message(key, instance, purpose, payload):
    raw = json.dumps(payload, separators=(',', ':'), ensure_ascii=False).encode()
    if len(raw) > MAX_REMOTE_ENVELOPE - 1024:
        raise ValueError('Gateway envelope exceeds the transport limit')
    nonce = secrets.token_bytes(12)
    encrypted = AESGCM(key).encrypt(nonce, raw, f'{instance}:{purpose}'.encode())
    encoded = base64.b64encode(nonce + encrypted)
    if len(encoded) > MAX_REMOTE_ENVELOPE:
        raise ValueError('Gateway envelope exceeds the transport limit')
    return encoded


def decrypt_message(key, instance, purpose, envelope):
    if len(envelope) > MAX_REMOTE_ENVELOPE:
        raise ValueError('Gateway envelope exceeds the transport limit')
    raw = base64.b64decode(envelope, validate=True)
    result = json.loads(AESGCM(key).decrypt(raw[:12], raw[12:], f'{instance}:{purpose}'.encode()))
    if not isinstance(result, dict):
        raise ValueError('Gateway envelope must be an object')
    return result
