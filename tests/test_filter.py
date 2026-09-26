import pytest

import grab


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("*.Example.COM.", "example.com"),
        ("  SLOT88.com  ", "slot88.com"),
        ("a-b.example.co.uk", "a-b.example.co.uk"),
        ("*.münchen.example", "xn--mnchen-3ya.example"),
        ("sub.domain.example", "sub.domain.example"),
    ],
)
def test_normalize_domain_valid(raw, expected):
    assert grab.normalize_domain(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        "",
        None,
        42,
        "nodot",
        "a b.com",
        "1.2.3.4",
        "..",
        "bad..example.com",
        "exa mple.com",
        "x" * 254 + ".com",
    ],
)
def test_normalize_domain_invalid(raw):
    assert grab.normalize_domain(raw) is None


@pytest.mark.parametrize(
    "domain,expected",
    [
        ("slot88.example.com", True),
        ("toto-togel.xyz", True),
        ("rtp-slot-gacor.net", True),
        ("gacor-malam.org", True),
        ("maxwin777.com", True),
        ("judi-online.id", True),
        ("casino88.com", True),
        ("jackpot168.site", True),
        # rtp: diawali non-huruf -> kena
        ("rtp888.example.com", True),
        ("live-rtp.example.com", True),
        # rtp: tertanam di tengah kata -> ditolak (FP Salesforce/AWS)
        ("0digicertplxef2.sfdc.net", False),
        ("mquswwbkykazmazortp.fis.example.net", False),
        ("myrtp.example.com", False),
        # kata panjang tetap substring murni (recall judol)
        ("superslot4154.ph", True),
        ("gacorslot138.example.com", True),
        ("vegasjackpots777.example.com", True),
        # di luar daftar kuat -> ditolak
        ("better.example.com", False),
        ("example.com", False),
        ("deposit-free.app", False),
        ("bandarq.example.com", False),
        ("wdcepat.example.com", False),
    ],
)
def test_is_candidate(domain, expected):
    assert grab.is_candidate(domain) is expected


def test_keyword_case_insensitive():
    assert grab.is_candidate("ToGeL88.XYZ")
    assert grab.is_candidate("RTP-GACOR.SITE")
