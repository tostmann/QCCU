#!/usr/bin/env python3
"""Aufkleber → 16-Byte-Geraeteschluessel."""

KEY_CHARS = "0123456789ABCEFGHJKLMNPQRSTUWXYZ"


def sticker_to_local_key(sticker: str) -> bytes:
    s = sticker.replace("-", "").upper()
    arr = bytearray(16)
    bits = 0
    j = len(s) - 1
    shift = 0
    out_pos = len(arr) - 1
    while j >= 0:
        c = s[j]
        if c not in KEY_CHARS:
            raise ValueError(f"Char '{c}' not in alphabet")
        i = KEY_CHARS.index(c)
        bits |= i << shift
        shift += 5
        j -= 1
        while shift > 8 and out_pos >= 0:
            arr[out_pos] = bits & 0xFF
            bits >>= 8
            shift -= 8
            out_pos -= 1
    return bytes(arr)


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1:
        for sticker in sys.argv[1:]:
            key = sticker_to_local_key(sticker)
            print(f"{sticker}\t-> {key.hex()}")
    else:
        cases = [
            ("ABCEF-GHJKL-MNPQR-STUWX-YZ2345", "4B635CF84653A56D7C675BE77DF10C85"),
            ("22222-33333-44444-55555-666666", "421084318C6321084214A5294C6318C6"),
        ]
        for sticker, expected in cases:
            got = sticker_to_local_key(sticker).hex().upper()
            tag = "OK" if (expected is None or got == expected) else "FAIL"
            print(f"[{tag}] {sticker} -> {got}" + (f"  (expected {expected})" if expected else ""))
