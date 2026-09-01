"""Minimal ctypes binding to libopus (DR-004).

Why not pip opuslib: unmaintained (2018), fails to build on py3.11+.
Surface: mono encoder/decoder at 8/16/48 kHz with the ctls this project
needs (bitrate, DTX, in-band FEC, expected loss, VOIP signal, complexity)
plus PLC and FEC decode paths. Tested by S-02.
"""
from __future__ import annotations
import ctypes, ctypes.util
import numpy as np

_libname = ctypes.util.find_library("opus") or "libopus.so.0"
_lib = ctypes.CDLL(_libname)

OPUS_APPLICATION_VOIP = 2048
OPUS_OK = 0
# ctl request codes (opus_defines.h)
OPUS_SET_BITRATE = 4002
OPUS_SET_COMPLEXITY = 4010
OPUS_SET_INBAND_FEC = 4012
OPUS_SET_PACKET_LOSS_PERC = 4014
OPUS_SET_DTX = 4016
OPUS_SET_SIGNAL = 4024
OPUS_SIGNAL_VOICE = 3001

_lib.opus_encoder_create.restype = ctypes.c_void_p
_lib.opus_decoder_create.restype = ctypes.c_void_p
_lib.opus_strerror.restype = ctypes.c_char_p


class OpusError(RuntimeError):
    pass


def _check(code: int, ctx: str) -> None:
    if code < 0:
        raise OpusError(f"{ctx}: {_lib.opus_strerror(code).decode()}")


class Encoder:
    def __init__(self, fs: int = 16000, bitrate: int = 16000, dtx: bool = True,
                 fec: bool = True, loss_pct: int = 10, complexity: int = 5):
        err = ctypes.c_int()
        self._st = _lib.opus_encoder_create(fs, 1, OPUS_APPLICATION_VOIP,
                                            ctypes.byref(err))
        _check(err.value, "encoder_create")
        self.fs = fs
        ctl = _lib.opus_encoder_ctl
        _check(ctl(ctypes.c_void_p(self._st), OPUS_SET_SIGNAL, OPUS_SIGNAL_VOICE), "signal")
        _check(ctl(ctypes.c_void_p(self._st), OPUS_SET_BITRATE, int(bitrate)), "bitrate")
        _check(ctl(ctypes.c_void_p(self._st), OPUS_SET_DTX, int(dtx)), "dtx")
        _check(ctl(ctypes.c_void_p(self._st), OPUS_SET_INBAND_FEC, int(fec)), "fec")
        _check(ctl(ctypes.c_void_p(self._st), OPUS_SET_PACKET_LOSS_PERC, int(loss_pct)), "loss")
        _check(ctl(ctypes.c_void_p(self._st), OPUS_SET_COMPLEXITY, int(complexity)), "cplx")

    def encode(self, pcm: np.ndarray) -> bytes:
        assert pcm.dtype == np.int16 and pcm.ndim == 1
        out = ctypes.create_string_buffer(4000)
        n = _lib.opus_encode(ctypes.c_void_p(self._st),
                             pcm.ctypes.data_as(ctypes.POINTER(ctypes.c_int16)),
                             len(pcm), out, 4000)
        _check(n, "encode")
        return out.raw[:n]


class Decoder:
    def __init__(self, fs: int = 16000):
        err = ctypes.c_int()
        self._st = _lib.opus_decoder_create(fs, 1, ctypes.byref(err))
        _check(err.value, "decoder_create")
        self.fs = fs

    def decode(self, data: bytes | None, frame: int, fec: bool = False) -> np.ndarray:
        """data=None -> packet-loss concealment for `frame` samples."""
        out = np.zeros(frame, dtype=np.int16)
        buf = None if data is None else (ctypes.c_char * len(data)).from_buffer_copy(data)
        n = _lib.opus_decode(ctypes.c_void_p(self._st), buf,
                             0 if data is None else len(data),
                             out.ctypes.data_as(ctypes.POINTER(ctypes.c_int16)),
                             frame, int(fec))
        _check(n, "decode")
        return out[:n]
