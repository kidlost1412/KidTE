"""Pinned public keys used to verify update catalogs.

Private signing keys are kept outside this repository and protected with DPAPI.
"""

# Public release key pinned on 2026-10-10. The matching DPAPI-protected private
# key is kept outside the checkout by the release owner.
TRUSTED_KEYS: dict[str, str] = {
    "f742ddd2de3f6f90": "2kNHqQfNcnc+wQWLIdZv5F+UnTCsuNWTxD/u9mhcJdM=",
}
