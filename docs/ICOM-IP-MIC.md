# Icom IP remote mic protocol (IC-F6330D)

Reverse engineered from captures between an IC-F6330D and its PoE IP mic head.
`server/icom_ipmic.py` implements
it; `server/icom.py` is RadPi's `radio.type: icom` built on it (README, "Icom IP mic radio"), and
`python -m server.icom_cli` is a stand-alone mic you can drive from a terminal.

## Transport

| | |
|---|---|
| Control | TCP 52001. **The radio connects to the mic.** It retries every few seconds until it gets in, even while switched off. |
| Audio | RTP over UDP 50000 ↔ 50000, payload type 125, 16-bit signed **big-endian** PCM, 8 kHz mono, 160 samples (20 ms) a packet. No codec. |
| Security | None: no login, no challenge, no encryption. Keep it on a private network or VPN. |

## Frames

```
f3 41 71 | group | cmd | len (2, BE) | data | crc (2, BE) | fd
```

- `crc` is CRC-16/MODBUS (poly 0xA001 reflected, init 0xFFFF) over `41 71 … data`.
- Any byte `f0`–`ff` between the start byte and `fd` is sent as `ff 0X`: `fd` becomes `ff 0d`, and `ff` becomes `ff 0f`.
- Frames that start with `f5` must be acknowledged with `f5 41 71 ee <group> <cmd> 01 <crc> fd`. They're only used for session teardown.

## Session

| Dir | Frame | Meaning |
|---|---|---|
| R→M | `05 05 [01]` | Hello |
| M→R | `01 0a [00]`, `01 00 [00]`, `01 01 [7f]`, `01 01 [1f]`, `05 06 [00]` | Mic state: PTT up, no keys held |
| R→M | `05 01 [01]` | Asks for the mic's ID |
| M→R | `05 02 [26 bytes]` | ID: `01 01 01 ff ff ff ff 60 cc` "Icom Inc" `01`, the mic's MAC address (6 bytes, `00 90 c7 …`), then 2 unknown bytes. The radio doesn't seem to check it. The mic also sends one silent RTP packet |
| R→M | `02 06`, `02 0b`, `01 04`, `01 08`, `02 02`, `02 01`, `06 09` (32 bytes), `06 0a` (10-step table, probably the volume curve), `02 0d`, `02 0e` | Startup (power on) |
| R→M / M→R | `05 00 [4d 00]` / `05 00 [6d 01]` | Keepalive every 2 s, from the radio; the mic answers |
| R→M | `f5 … 05 03 [01 or 02]` | Radio closing the session: `01` = powering on (it reconnects with the full startup), `02` = powering off (it reconnects to a blank radio) |
| M→R | `f5 … 05 04 [01]` | Mic's goodbye, then both close TCP |

## Mic → radio

| Frame | Meaning |
|---|---|
| `01 01 [k]` | Key down. Release is `k\|80`, then `7f` (nothing held) |
| `01 00 [01]` / `[00]` | PTT down / up |
| `01 09 [01]` / `[00]` | Power key down / up (hold about 2 s to turn off) |
| `02 09 [01]` | Acknowledges each display update (`02 08`) |

Key codes (a matrix: high nibble = row):

| | x0 | x1 | x2 | x3 | x4 | x5 |
|---|---|---|---|---|---|---|
| 0x | SIDE (squelch) | VOL− | P1 (shift) | 1 | 2 | 3 |
| 1x | VOL+ | | P2 (talkaround) | 4 | 5 | 6 |
| 2x | UP (ch+) | DOWN (ch−) | P3 (scan) | 7 | 8 | 9 |
| 3x | LEFT (zone−) | RIGHT (zone+) | P4 (send call) | * | 0 | # |
| 4x | EMERG (menu, hold) | | | | | |

The functions are this radio's programming. Typing three digits jumps straight to that channel number, shown as " ch12_" while you type.

## Radio → mic

| Frame | Meaning |
|---|---|
| `02 07 [02]`, `02 0a [68]`, `02 08 [00 44]` | Display update. In `02 0a`, bytes 0–7 are the text; a set top bit means "this character followed by a decimal point" (`b4` = "4."). The bytes after the text are icon flags (shift, talkaround, scan, busy). |
| `02 02 [x]` | LED: `02` red (transmitting), `04` green (busy or squelch open), `06` both |
| `01 04 [f 00 00 00]` | Audio routing: `01` = radio is sending speaker audio, `08` = radio is transmitting and wants mic audio |
| `02 0b [x]` | Backlight: `02` on (any key), `00` off about 10 s later |
| `02 0e [04/05]` | Toggles while scanning (blinking icon) |

## Flows

**Transmit:**
1. Mic sends `01 00 [01]`.
2. Radio sends `01 04 [08]` and the red LED flashes.
3. Mic streams RTP.
4. Mic sends `01 00 [00]`.
5. Radio sends `01 04 [00]` and the mic stops streaming.

P4 ("send call") makes the radio transmit by itself: it sends `01 04 [08]` without any PTT, and the mic streams until `01 04 [00]`.

**Receive:** the radio sends `01 04 [01]`, `02 02 [04]` and a display refresh with the busy icon, then streams RTP until `01 04 [00]` and `02 02 [00]`.

**Levels:** the real mic peaks around −18 dBFS and is about −40 dBFS RMS. Receive audio is about −43 dBFS RMS. Send at mic level or the radio will over-deviate.

**Not yet seen:** the menu (`EMERG` held), and what the radio sends if the TCP link drops in the middle of a transmission.
