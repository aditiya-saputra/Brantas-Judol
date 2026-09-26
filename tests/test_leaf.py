import base64
import json
import pathlib

import pytest

import grab

FIXTURE_DIR = pathlib.Path(__file__).parent / "fixtures"
FIXTURES = sorted(FIXTURE_DIR.glob("*.json"))


def load_fixture(path: pathlib.Path) -> dict:
    return json.loads(path.read_text())


@pytest.mark.parametrize("path", FIXTURES, ids=lambda p: p.stem)
def test_parse_leaf_matches_fixture(path):
    fx = load_fixture(path)
    leaf = grab.parse_leaf(base64.b64decode(fx["leaf_input"]))

    assert leaf.version == 0
    assert leaf.leaf_type == 0
    assert leaf.timestamp == fx["timestamp"]
    assert leaf.entry_type == fx["entry_type"]
    assert leaf.extensions == b""

    if fx["entry_type"] == 0:
        assert leaf.cert_der, "x509 entry harus punya cert_der"
        assert leaf.tbs_der == b""
    else:
        assert leaf.cert_der == b""
        assert leaf.tbs_der, "precert entry harus punya tbs_der"


@pytest.mark.parametrize("path", FIXTURES, ids=lambda p: p.stem)
def test_domains_from_entry_matches_fixture(path):
    fx = load_fixture(path)
    entry = {"leaf_input": fx["leaf_input"], "extra_data": fx["extra_data"]}
    assert grab.domains_from_entry(entry) == set(fx["domains"])


@pytest.mark.parametrize("path", FIXTURES, ids=lambda p: p.stem)
def test_parse_extra_returns_full_precertificate(path):
    fx = load_fixture(path)
    if fx["entry_type"] != 1:
        pytest.skip("bukan precert entry")
    extra = base64.b64decode(fx["extra_data"])
    pre_certificate, chain = grab.parse_extra(extra)
    # pre_certificate harus sertifikat penuh (bukan sekadar TBSCertificate)
    assert pre_certificate[:1] == b"\x30"
    assert len(pre_certificate) > 100
    assert chain, "precert harus punya chain issuer"
    # cert dari pre_certificate harus menghasilkan domain yang sama
    assert grab.domains_from_cert(pre_certificate) == set(fx["domains"])


def test_fixture_set_is_complete():
    entry_types = {load_fixture(p)["entry_type"] for p in FIXTURES}
    assert 0 in entry_types, "butuh minimal satu fixture x509_entry"
    assert 1 in entry_types, "butuh minimal satu fixture precert_entry"


def test_unknown_entry_type_raises():
    leaf = bytes([0, 0]) + (1).to_bytes(8, "big") + (99).to_bytes(2, "big") + b"\x00\x00"
    with pytest.raises(ValueError, match="entry_type"):
        grab.parse_leaf(leaf)


def test_truncated_leaf_raises():
    with pytest.raises(ValueError):
        grab.parse_leaf(b"\x00\x00")
