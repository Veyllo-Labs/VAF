# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""The test vectors of the app link protocol v1, from fixed bytes through the code that runs.

    venv/bin/python scripts/app_link_vectors.py           # write tests/fixtures/app_link/
    venv/bin/python scripts/app_link_vectors.py --check   # exit 1 when the files drifted

The app side checks its own encoder against these files; tests/test_app_link_protocol.py
checks that the files equal this script's output, so a change of the encoder that is not
also a change of the vectors fails.
"""
from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path
from typing import Any, Dict

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

FIXTURES = ROOT / "tests" / "fixtures" / "app_link"

K = bytes(range(32))
P = bytes(range(32, 64))
CA = hashlib.sha256(b"VAF app link test vector CA").digest()
EXP = 1794000000
SCALARS = (1, 0x0123456789ABCDEF0123456789ABCDEF0123456789ABCDEF0123456789ABCDEF,
           0x7F7F7F7F7F7F7F7F7F7F7F7F7F7F7F7F7F7F7F7F7F7F7F7F7F7F7F7F7F7F7F7F)


def _qr() -> Dict[str, Any]:
    from vaf.core.app_link.protocol import b64url, build_qr
    cases = [
        ("relay_without_direct", dict(mode="r", p=P, api="https://veyllo.app")),
        ("relay_with_direct", dict(mode="r", p=P, api="https://veyllo.app",
                                   direct=["wss://10.66.66.1:8443/api/app-link"],
                                   name="Firma XY")),
        ("local_two_direct", dict(mode="l", direct=["wss://192.168.1.20:8443/api/app-link",
                                                    "wss://[fd00::20]:8443/api/app-link"],
                                  name="Büro Müller")),
    ]
    vectors = []
    for name, kwargs in cases:
        args = {key: (b64url(value) if isinstance(value, bytes) else value)
                for key, value in kwargs.items()}
        vectors.append({"name": name, "input": {**args, "k": b64url(K), "ca_sha256": b64url(CA),
                                                "exp": EXP},
                        "qr": build_qr(k=K, ca_sha256=CA, exp=EXP, **kwargs)})
    return {"version": 1, "note": "inputs base64url without padding; build_qr(**input) == qr",
            "vectors": vectors}


def _frames() -> Dict[str, Any]:
    from vaf.core.app_link.protocol import CHUNK_DATA_MAX, encode_chunk, encode_json, wrap_relay
    json_obj = {"t": "msg", "id": "0f8fad5b-d9cb-469f-a165-70867728950e",
                "chat": "veyllo_app_00112233aabb", "text": "Hallo ü"}
    xfer = bytes.fromhex("11" * 16)
    big = bytes((i * 7) & 0xFF for i in range(CHUNK_DATA_MAX))
    big_frame = encode_chunk(xfer, 3, big)
    small = encode_chunk(xfer, 0, b"abc")
    cid = bytes.fromhex("00112233445566778899aabbccddeeff")
    tls = bytes.fromhex("1703030005") + b"hello"
    return {"version": 1, "vectors": [
        {"name": "json", "obj": json_obj, "hex": encode_json(json_obj).hex()},
        {"name": "chunk_small", "xfer": xfer.hex(), "index": 0, "data_hex": b"abc".hex(),
         "hex": small.hex()},
        {"name": "chunk_full", "xfer": xfer.hex(), "index": 3,
         "data": "bytes((i * 7) & 0xFF for i in range(1015808))",
         "length": len(big_frame), "sha256": hashlib.sha256(big_frame).hexdigest()},
        {"name": "relay_envelope", "cid": cid.hex(), "tls_hex": tls.hex(),
         "hex": wrap_relay(cid, tls).hex()},
    ]}


def _fingerprints() -> Dict[str, Any]:
    from cryptography.hazmat.primitives.asymmetric import ec

    from vaf.core.app_link.pki import display_fingerprint, spki_der
    vectors = []
    for scalar in SCALARS:
        key = ec.derive_private_key(scalar, ec.SECP256R1()).public_key()
        der = spki_der(key)
        vectors.append({"private_scalar_hex": f"{scalar:064x}", "spki_der_hex": der.hex(),
                        "spki_sha256": hashlib.sha256(der).hexdigest(),
                        "display": display_fingerprint(key)})
    return {"version": 1, "note": "P-256; display = first 8 bytes of SHA-256(SPKI DER), "
                                  "uppercase hex, 4 groups", "vectors": vectors}


def _relay() -> Dict[str, Any]:
    from vaf.core.app_link.protocol import MAX_TLS_BYTES_PER_FRAME, split_tls
    cid = bytes.fromhex("ffeeddccbbaa99887766554433221100")
    return {"version": 1, "vectors": {
        "envelope_empty": {"cid": cid.hex(), "hex": (b"\x01" + cid).hex()},
        "split_2mib": {"total": 2 * 1048576,
                       "pieces": [len(p) for p in split_tls(bytes(2 * 1048576))],
                       "max_piece": MAX_TLS_BYTES_PER_FRAME},
        "refused": [{"why": "short", "hex": ("01" + "00" * 15)},
                    {"why": "head", "hex": ("02" + "00" * 16 + "aa")},
                    {"why": "empty", "hex": ""}],
    }}


def build() -> Dict[str, Dict[str, Any]]:
    return {"qr_vectors.json": _qr(), "frame_vectors.json": _frames(),
            "fingerprint_vectors.json": _fingerprints(), "relay_vectors.json": _relay()}


def render(obj: Dict[str, Any]) -> bytes:
    return (json.dumps(obj, indent=2, ensure_ascii=False) + "\n").encode("utf-8")


def main(argv) -> int:
    check = "--check" in argv
    drift = []
    FIXTURES.mkdir(parents=True, exist_ok=True)
    for name, obj in build().items():
        path = FIXTURES / name
        data = render(obj)
        if check:
            if not path.exists() or path.read_bytes() != data:
                drift.append(name)
        else:
            path.write_bytes(data)
    if drift:
        print("app link vectors drifted: " + ", ".join(drift))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
