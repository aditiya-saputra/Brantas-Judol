import pytest

import grab


def test_integer_reads():
    data = bytes(
        [0xAB]
        + [0x12, 0x34]
        + [0x12, 0x34, 0x56]
        + list((0x0102030405060708).to_bytes(8, "big"))
        + [0x00, 0x03, ord("a"), ord("b"), ord("c")]
        + [0x00, 0x00, 0x02, ord("d"), ord("e")]
    )
    r = grab.TLSReader(data)

    assert r.u8() == 0xAB
    assert r.u16() == 0x1234
    assert r.u24() == 0x123456
    assert r.u64() == 0x0102030405060708
    assert r.var16() == b"abc"
    assert r.var24() == b"de"
    assert r.pos == len(data)


def test_u64_big_endian():
    r = grab.TLSReader((1).to_bytes(8, "big"))
    assert r.u64() == 1
    r = grab.TLSReader(b"\xff" * 8)
    assert r.u64() == 2**64 - 1


def test_short_read_raises_value_error():
    with pytest.raises(ValueError):
        grab.TLSReader(b"\x01").u16()
    with pytest.raises(ValueError):
        grab.TLSReader(b"").u8()
    with pytest.raises(ValueError):
        grab.TLSReader(b"\x01\x02").u64()


def test_var_length_beyond_buffer_raises():
    with pytest.raises(ValueError):
        grab.TLSReader(b"\x00\x05ab").var16()
    with pytest.raises(ValueError):
        grab.TLSReader(b"\x00\x00\x10").var24()
    with pytest.raises(ValueError):
        grab.TLSReader(b"").var24()
