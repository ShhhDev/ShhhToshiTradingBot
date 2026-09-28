"""
TON address helpers (no external dependencies).

TON addresses come in two spellings for the same account:
  * raw:      "0:b113a994...dfe"            (workchain:hex, 66 chars)
  * friendly: "EQCxE6mU...Ds"               (base64url, always 48 chars)

TonAPI returns raw addresses, users paste friendly ones, and Telegram
button data is capped at 64 bytes (a raw address alone is 66), so the rest
of the bot always DISPLAYS / puts into buttons the 48-char friendly form
and COMPARES addresses through same() so the two spellings match.
"""

import base64
import binascii
import re

_RAW_RE = re.compile(r"^(-?\d+):([0-9a-fA-F]{64})$")


def _decode_friendly(s: str):
    if len(s) != 48:
        return None
    try:
        data = base64.urlsafe_b64decode(s)
    except (binascii.Error, ValueError):
        return None
    if len(data) != 36:
        return None
    body, crc = data[:34], int.from_bytes(data[34:], "big")
    if binascii.crc_hqx(body, 0) != crc:
        return None
    wc = data[1] - 256 if data[1] > 127 else data[1]
    return wc, bytes(data[2:34])


def parse(addr):
    """Return (workchain, 32-byte hash) for a raw or friendly address, else None."""
    if not isinstance(addr, str):
        return None
    s = addr.strip()
    m = _RAW_RE.match(s)
    if m:
        return int(m.group(1)), bytes.fromhex(m.group(2))
    return _decode_friendly(s)


def is_valid(addr) -> bool:
    return parse(addr) is not None


def to_raw(addr):
    p = parse(addr)
    if p is None:
        return None
    return f"{p[0]}:{p[1].hex()}"


def to_friendly(addr, bounceable: bool = True, testnet: bool = False):
    p = parse(addr)
    if p is None:
        return None
    wc, h = p
    tag = 0x11 if bounceable else 0x51
    if testnet:
        tag |= 0x80
    body = bytes([tag, wc & 0xFF]) + h
    crc = binascii.crc_hqx(body, 0).to_bytes(2, "big")
    return base64.urlsafe_b64encode(body + crc).decode()


def same(a, b) -> bool:
    pa, pb = parse(a), parse(b)
    return pa is not None and pa == pb


def canonical(addr):
    """Friendly bounceable form used for display, buttons and API lookups.
    Falls back to the input unchanged if it can't be parsed."""
    return to_friendly(addr) or addr
