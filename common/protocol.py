"""C-0 wire protocol: RTP framing + control-plane envelopes (plan §03).

Media: Opus mono, ptime 60 ms, PT 111, 48 kHz RTP clock (INV-7/8).
Uplink: every bridge -> mixer :5100, identified by SSRC = crc32(node_id)
(DR-005). Downlink: mixer -> bridge :6100+2n from roster.
Control: JSON envelope {v,t,ts,from,data} over WebSocket :8800. Snapshots are
authoritative; deltas are conveniences (INV-7).
"""
from __future__ import annotations
import json
import struct
import time
import zlib

RTP_PT = 111
MIXER_RTP_PORT = 5100
CONTROL_PORT = 8800
PROTO_V = 1

MSG_TYPES = {"hello", "snapshot", "heartbeat", "vad", "move", "lead_transfer",
             "duck", "text", "tts", "gps", "bye",
             "node_cmd",   # base->node remote debug: reboot/reconnect/volume/identify/settings
             "ack",        # node->base command result {cmd_id, ok, detail}
             "audio_ctl"}  # ui->base: {pid, mute?|trim?} composed with the ladder


def ssrc_of(node_id: str) -> int:
    return zlib.crc32(node_id.encode()) & 0xFFFFFFFF


def rtp_pack(seq: int, ts: int, ssrc: int, payload: bytes, pt: int = RTP_PT) -> bytes:
    hdr = struct.pack("!BBHII", 0x80, pt & 0x7F, seq & 0xFFFF, ts & 0xFFFFFFFF, ssrc)
    return hdr + payload


def rtp_unpack(pkt: bytes) -> tuple[int, int, int, int, bytes]:
    """-> (pt, seq, ts, ssrc, payload). Raises ValueError on junk."""
    if len(pkt) < 12 or (pkt[0] >> 6) != 2:
        raise ValueError("not RTP v2")
    b0, b1, seq, ts, ssrc = struct.unpack("!BBHII", pkt[:12])
    off = 12 + 4 * (b0 & 0x0F)
    return b1 & 0x7F, seq, ts, ssrc, pkt[off:]


def make_msg(t: str, sender: str, data: dict) -> str:
    assert t in MSG_TYPES, t
    return json.dumps({"v": PROTO_V, "t": t, "ts": round(time.time(), 3),
                       "from": sender, "data": data})


def parse_msg(raw: str | bytes) -> dict:
    m = json.loads(raw)
    if m.get("v") != PROTO_V:
        raise ValueError(f"protocol version {m.get('v')} != {PROTO_V}")
    if m.get("t") not in MSG_TYPES:
        raise ValueError(f"unknown msg type {m.get('t')}")
    return m
