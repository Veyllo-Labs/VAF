# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""The app link wire format against the vectors the app side checks its own code with."""
import hashlib
import importlib.util
import json
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric import ec

from vaf.core.app_link import protocol as pr
from vaf.core.app_link.pki import display_fingerprint, spki_der

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = Path(__file__).resolve().parent / "fixtures" / "app_link"


def _load(name):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


# ── QR ───────────────────────────────────────────────────────────────────────

def test_qr_vectors():
    for vector in _load("qr_vectors.json")["vectors"]:
        args = dict(vector["input"])
        mode = args.pop("mode")
        for key in ("p", "k", "ca_sha256"):
            if key in args:
                args[key] = pr.b64url_decode(args[key])
        assert pr.build_qr(mode, **args) == vector["qr"], vector["name"]
        parsed = pr.parse_qr(vector["qr"])
        assert parsed["k"] == args["k"] and parsed["ca"] == args["ca_sha256"]
        assert parsed["d"] == list(args.get("direct", []))
        assert parsed.get("n", "") == args.get("name", "")
        assert parsed["exp"] == args["exp"]
        if mode == "r":
            assert parsed["p"] == args["p"] and parsed["api"] == args["api"]
        # An unknown field is ignored.
        assert pr.parse_qr(vector["qr"] + "&zz=1")["k"] == args["k"]


def test_a_name_with_plus_and_comma_is_a_vector():
    """The app side asked how "+" and "," in a name travel: %2B and %2C, never literal."""
    names = {v["name"]: v for v in _load("qr_vectors.json")["vectors"]}
    vector = names["local_name_plus_comma"]
    assert vector["input"]["name"] == "R+D, Lab 2"
    assert "&n=R%2BD%2C%20Lab%202&" in vector["qr"]
    assert pr.parse_qr(vector["qr"])["n"] == "R+D, Lab 2"


def test_qr_direct_entries_keep_their_commas_encoded():
    qr = pr.build_qr("l", k=bytes(32), ca_sha256=bytes(32), exp=1,
                     direct=["wss://a:1/x,y", "wss://b:2/y"])
    fragment = qr.split("#", 1)[1]
    d = dict(part.split("=", 1) for part in fragment.split("&"))["d"]
    # The separator is a literal comma; a comma inside an entry is encoded.
    assert d.count(",") == 1 and d.count("%2C") == 1
    assert pr.parse_qr(qr)["d"] == ["wss://a:1/x,y", "wss://b:2/y"]


def test_qr_mode_rules():
    k, ca = bytes(32), bytes(32)
    with pytest.raises(ValueError):
        pr.build_qr("r", k=k, ca_sha256=ca, exp=1, api="https://veyllo.app")
    with pytest.raises(ValueError):
        pr.build_qr("r", k=k, ca_sha256=ca, exp=1, p=bytes(32))
    with pytest.raises(ValueError):
        pr.build_qr("l", k=k, ca_sha256=ca, exp=1, p=bytes(32), direct=["wss://a/x"])
    with pytest.raises(ValueError):
        pr.build_qr("l", k=k, ca_sha256=ca, exp=1, api="https://x", direct=["wss://a/x"])
    with pytest.raises(ValueError):
        pr.build_qr("l", k=k, ca_sha256=ca, exp=1)
    with pytest.raises(ValueError):
        pr.build_qr("r", k=bytes(31), ca_sha256=ca, exp=1, p=bytes(32), api="https://x")
    good = pr.build_qr("l", k=k, ca_sha256=ca, exp=1, direct=["wss://a/x"])
    with pytest.raises(ValueError):
        pr.parse_qr(good.replace("&exp=1", ""))
    with pytest.raises(ValueError):
        pr.parse_qr(good.replace("m=l", "m=r"))
    with pytest.raises(ValueError):
        pr.parse_qr(good.replace("k=" + pr.b64url(k), "k=" + pr.b64url(bytes(31))))


def test_qr_name_is_cut_on_character_boundaries():
    seventy = "a" * 70
    assert pr.cut_name(seventy) == "a" * 64
    four_byte = "\U0001F600" * 40
    cut = pr.cut_name(four_byte)
    assert cut == "\U0001F600" * 32 and len(cut.encode("utf-8")) == 128
    mixed = "ab" + "\U0001F600" * 40
    cut = pr.cut_name(mixed)
    assert len(cut.encode("utf-8")) <= 128 and cut.encode("utf-8").decode("utf-8") == cut
    assert cut == "ab" + "\U0001F600" * 31
    qr = pr.build_qr("l", k=bytes(32), ca_sha256=bytes(32), exp=1, direct=["wss://a/x"],
                     name=four_byte)
    assert pr.parse_qr(qr)["n"] == "\U0001F600" * 32


# ── frames ───────────────────────────────────────────────────────────────────

def test_frame_vectors():
    vectors = {v["name"]: v for v in _load("frame_vectors.json")["vectors"]}
    assert pr.encode_json(vectors["json"]["obj"]).hex() == vectors["json"]["hex"]
    small = vectors["chunk_small"]
    assert pr.encode_chunk(bytes.fromhex(small["xfer"]), small["index"],
                           bytes.fromhex(small["data_hex"])).hex() == small["hex"]
    full = vectors["chunk_full"]
    data = bytes((i * 7) & 0xFF for i in range(pr.CHUNK_DATA_MAX))
    frame = pr.encode_chunk(bytes.fromhex(full["xfer"]), full["index"], data)
    assert len(frame) == full["length"] and hashlib.sha256(frame).hexdigest() == full["sha256"]
    env = vectors["relay_envelope"]
    wrapped = pr.wrap_relay(bytes.fromhex(env["cid"]), bytes.fromhex(env["tls_hex"]))
    assert wrapped.hex() == env["hex"]
    assert pr.unwrap_relay(wrapped) == (bytes.fromhex(env["cid"]), bytes.fromhex(env["tls_hex"]))
    with pytest.raises(pr.FrameError):
        pr.encode_chunk(bytes(16), 0, bytes(pr.CHUNK_DATA_MAX + 1))


def test_relay_vectors():
    vectors = _load("relay_vectors.json")["vectors"]
    empty = vectors["envelope_empty"]
    assert pr.unwrap_relay(bytes.fromhex(empty["hex"])) == (bytes.fromhex(empty["cid"]), b"")
    split = vectors["split_2mib"]
    assert [len(p) for p in pr.split_tls(bytes(split["total"]))] == split["pieces"]
    assert max(split["pieces"]) == pr.MAX_TLS_BYTES_PER_FRAME == 1048559
    for bad in vectors["refused"]:
        with pytest.raises(pr.FrameError):
            pr.unwrap_relay(bytes.fromhex(bad["hex"]))


def test_reader_every_cut():
    stream = (pr.encode_json({"t": "ack", "id": "0f8fad5b-d9cb-469f-a165-70867728950e"})
              + pr.encode_chunk(b"\x22" * 16, 7, b"payload-bytes")
              + pr.encode_json({"t": "bye"}))
    for cut in range(len(stream) + 1):
        reader = pr.FrameReader()
        frames = reader.feed(stream[:cut]) + reader.feed(stream[cut:])
        assert [f.type for f in frames] == [1, 2, 1], cut
        assert frames[0].obj["t"] == "ack" and frames[2].obj == {"t": "bye"}
        assert frames[1].xfer == b"\x22" * 16 and frames[1].index == 7
        assert frames[1].data == b"payload-bytes"
        assert reader.buffered == 0
    reader = pr.FrameReader()
    frames = []
    for byte in stream:
        frames += reader.feed(bytes([byte]))
    assert len(frames) == 3


def _raw(kind, payload):
    return bytes([kind]) + len(payload).to_bytes(4, "big") + payload


def test_reader_refusals():
    # The maximum itself is a valid length: no refusal is raised while it is incomplete.
    assert pr.FrameReader().feed(bytes([1]) + (pr.MAX_FRAME_PAYLOAD).to_bytes(4, "big")) == []
    for bad in (
        bytes([1]) + (pr.MAX_FRAME_PAYLOAD + 1).to_bytes(4, "big"),
        _raw(3, b"{}"),
        _raw(1, b"[1,2]"),
        _raw(1, b'{"t":"a","t":"b"}'),
        _raw(1, b'{"t":"a","x":{"y":{"z":{"w":{}}}}}'),
        _raw(1, b'{"t":1}'),
        _raw(1, b"\xff\xfe"),
        _raw(2, b"short"),
    ):
        with pytest.raises(pr.FrameError):
            pr.FrameReader().feed(bad)
    # Depth 4 is the limit, not over it.
    assert pr.FrameReader().feed(_raw(1, b'{"t":"a","x":{"y":{"z":{}}}}'))[0].obj["t"] == "a"
    with pytest.raises(pr.FrameError):
        pr.FrameReader().feed(_raw(1, b'{"t":"a","x":' + b"[" * 3000 + b"]" * 3000 + b"}"))


# ── fingerprints and the generator ───────────────────────────────────────────

def test_fingerprint_vectors():
    for vector in _load("fingerprint_vectors.json")["vectors"]:
        key = ec.derive_private_key(int(vector["private_scalar_hex"], 16),
                                    ec.SECP256R1()).public_key()
        assert spki_der(key).hex() == vector["spki_der_hex"]
        assert display_fingerprint(key) == vector["display"]
        assert vector["display"] == " ".join(
            vector["spki_sha256"][:16].upper()[i:i + 4] for i in range(0, 16, 4))
        assert len(vector["display"]) == 19


def test_vectors_match_generator():
    spec = importlib.util.spec_from_file_location("app_link_vectors",
                                                  ROOT / "scripts" / "app_link_vectors.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    for name, obj in module.build().items():
        assert (FIXTURES / name).read_bytes() == module.render(obj), name


# ── what the app may send ────────────────────────────────────────────────────

UID = "0f8fad5b-d9cb-469f-a165-70867728950e"


def test_validate_types_and_caps():
    assert isinstance(pr.validate_from_app({"t": "welcome"}), pr.UnknownType)
    assert isinstance(pr.validate_from_app({"t": "pair"}), pr.UnknownType)
    assert isinstance(pr.validate_from_app({"t": "msg"}, pr.PAIR_TYPES_FROM_APP), pr.UnknownType)
    msg = pr.validate_from_app({"t": "msg", "id": UID, "chat": "c", "text": "hi"})
    assert msg["text"] == "hi" and msg["attachments"] == []
    with pytest.raises(pr.ProtocolError):
        pr.validate_from_app({"t": "msg", "id": "nope", "chat": "c", "text": "hi"})
    with pytest.raises(pr.ProtocolError):
        pr.validate_from_app({"t": "msg", "id": UID, "chat": "c", "text": "x" * 65537})
    good_att = {"xfer": pr.b64url(bytes(16)), "name": "a.txt", "mime": "text/plain", "size": 3,
                "sha256": "a" * 64}
    for bad in ({"name": "../a"}, {"name": "a/b"}, {"size": 0}, {"size": 26214401},
                {"sha256": "A" * 64}, {"xfer": pr.b64url(bytes(15))}):
        with pytest.raises(pr.ProtocolError):
            pr.validate_from_app({"t": "msg", "id": UID, "chat": "c", "text": "",
                                  "attachments": [{**good_att, **bad}]})
    with pytest.raises(pr.ProtocolError):
        pr.validate_from_app({"t": "msg", "id": UID, "chat": "c", "text": "",
                              "attachments": [good_att] * 11})
    assert pr.validate_from_app({"t": "caps", "caps": ["calendar", "call"]})["caps"] == \
        ["calendar", "call"]
    for caps in (["Calendar"], ["a" * 33], ["x"] * 33, "calendar"):
        with pytest.raises(pr.ProtocolError):
            pr.validate_from_app({"t": "caps", "caps": caps})
    result = pr.validate_from_app({"t": "task_result", "id": UID, "ok": False,
                                   "error": "declined"})
    assert result["error"] == "declined"
    with pytest.raises(pr.ProtocolError):
        pr.validate_from_app({"t": "task_result", "id": UID, "ok": False, "error": "nah"})


def test_validate_pair():
    import base64
    base = {"t": "pair", "v": 1, "k": pr.b64url(bytes(32)), "csr": base64.b64encode(b"x" * 10).decode(),
            "device": "Pixel 9", "platform": "android", "app_version": "1.4.0"}
    out = pr.validate_from_app(base, pr.PAIR_TYPES_FROM_APP)
    assert out["csr"] == b"x" * 10
    for bad in ({"k": "short"}, {"device": "x" * 65}, {"device": "a\nb"},
                {"csr": base64.b64encode(b"x" * 4097).decode()}, {"csr": "!!"}, {"v": 2}):
        with pytest.raises(pr.ProtocolError):
            pr.validate_from_app({**base, **bad}, pr.PAIR_TYPES_FROM_APP)
