"""Software SBUS pipeline tests: synthetic waveform -> soft UART -> framer.

Requires numpy; skipped where it is not installed.
"""

import random

import pytest

np = pytest.importorskip("numpy")

from rpi_gateway.sbus_decoder import (  # noqa: E402
    SbusFramer,
    SoftUartDecoder,
    encode_sbus_frame,
    uart_encode_samples,
)

CHANNELS_A = [172, 992, 1811, 0, 2047, 1000, 1500, 500,
              992, 172, 1811, 992, 700, 1300, 992, 992]


def waveform(frames, spb, **kwargs):
    return uart_encode_samples(b"".join(frames), spb, **kwargs)


def decode_all(samples, spb, chunk=997, **kwargs):
    uart = SoftUartDecoder(spb, **kwargs)
    framer = SbusFramer()
    frames = []
    for i in range(0, len(samples), chunk):
        frames.extend(framer.feed(uart.feed(samples[i:i + chunk])))
    return frames, uart, framer


def test_soft_uart_decodes_plain_bytes():
    payload = bytes(range(256))
    samples = uart_encode_samples(payload, 10.0)
    uart = SoftUartDecoder(10.0)
    assert uart.feed(samples) == payload
    assert uart.stats.parity_errors == 0
    assert uart.stats.framing_errors == 0


def test_soft_uart_8n1_mode():
    payload = b"\xc8\x18\x16" + bytes(range(64))
    samples = uart_encode_samples(payload, 10.0, inverted=False,
                                  parity=None, stop_bits=1)
    uart = SoftUartDecoder(10.0, inverted=False, parity=None, stop_bits=1)
    assert uart.feed(samples) == payload


def test_soft_uart_fractional_rate_and_chunked_feed():
    payload = bytes(range(0, 250, 3)) * 4
    for spb in (9.6, 10.0, 10.4, 17.36):
        samples = uart_encode_samples(payload, spb)
        uart = SoftUartDecoder(spb)
        got = bytearray()
        for i in range(0, len(samples), 61):
            got.extend(uart.feed(samples[i:i + 61]))
        assert bytes(got) == payload, f"spb={spb}"


def test_soft_uart_rejects_bad_parity():
    # Encode with odd parity, decode expecting even: every byte dropped.
    payload = bytes([0x01, 0x02, 0x03])
    samples = uart_encode_samples(payload, 10.0, parity="odd")
    uart = SoftUartDecoder(10.0, parity="even")
    assert uart.feed(samples) == b""
    assert uart.stats.parity_errors == 3


def test_sbus_frame_roundtrip():
    frame = encode_sbus_frame(CHANNELS_A)
    assert len(frame) == 25
    assert frame[0] == 0x0F
    assert frame[24] == 0x00
    frames, _, framer = decode_all(waveform([frame], 10.0), 10.0)
    assert len(frames) == 1
    assert list(frames[0].channels) == CHANNELS_A
    assert not frames[0].failsafe and not frames[0].frame_lost
    assert framer.stats.frames_ok == 1


def test_sbus_flags_roundtrip():
    frame = encode_sbus_frame(CHANNELS_A, ch17=True, frame_lost=True,
                              failsafe=True)
    frames, _, _ = decode_all(waveform([frame], 10.0), 10.0)
    assert frames[0].ch17 and not frames[0].ch18
    assert frames[0].frame_lost and frames[0].failsafe


def test_sbus_stream_of_frames():
    rng = random.Random(7)
    channel_sets = [[rng.randrange(0, 2048) for _ in range(16)]
                    for _ in range(20)]
    wire = [encode_sbus_frame(ch) for ch in channel_sets]
    frames, _, _ = decode_all(waveform(wire, 10.0), 10.0)
    assert [list(f.channels) for f in frames] == channel_sets


def test_framer_resyncs_after_garbage_bytes():
    framer = SbusFramer()
    good = encode_sbus_frame(CHANNELS_A)
    frames = framer.feed(b"\x12\x34\x56\x0f\x99" + good + good)
    assert len(frames) == 2
    assert framer.stats.resync_bytes > 0
    for frame in frames:
        assert list(frame.channels) == CHANNELS_A


def test_decoder_survives_noise_and_recovers():
    rng = random.Random(99)
    noise = bytes(rng.randrange(256) for _ in range(5000))
    good = waveform([encode_sbus_frame(CHANNELS_A)] * 3, 10.0)
    uart = SoftUartDecoder(10.0)
    framer = SbusFramer()
    framer.feed(uart.feed(noise))          # must not raise
    frames = framer.feed(uart.feed(good))  # and must still decode after
    assert len(frames) >= 2
    assert list(frames[-1].channels) == CHANNELS_A


def test_spike_glitch_between_frames_is_tolerated():
    good = encode_sbus_frame(CHANNELS_A)
    samples = bytearray(waveform([good, good], 10.0))
    # Flip one sample in the inter-frame idle region (a 1-sample spike).
    idle_pos = len(samples) // 2
    samples[idle_pos] ^= 0x10
    frames, _, _ = decode_all(bytes(samples), 10.0)
    assert len(frames) >= 1
