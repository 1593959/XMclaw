"""Device-bound authentication (anti-req #8 — ClawJacked defense).

At pairing time, client and daemon exchange ed25519 public keys. Every WS
connection MUST sign a nonce with its device key; unsigned or unknown-key
connections are closed immediately.

Phase 2 deliverable.
"""
# Phase 2
