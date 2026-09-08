"""Transcribe a stereo capture locally with Whisper, one voice per channel.

Criterion C9 of POC #1 asks whether the captured audio is readable by a speech
engine on *both* voices. The two channels are deliberately transcribed
separately rather than mixed down: the left channel is the person at the
keyboard and the right channel is everyone else in the meeting, and keeping
them apart is what lets the pipeline attribute speech later (ADR-02).

Local Whisper is used while no AssemblyAI key is available. The result is a
measurement, not production code: the real pipeline goes through
``TranscriptionProvider`` (ADR-01).

Usage:
    python tools/transcribe_local.py capture.wav
    python tools/transcribe_local.py capture.wav --model medium --language fr
"""

from __future__ import annotations

import argparse
import struct
import sys
from dataclasses import dataclass
from pathlib import Path

CHANNEL_LABELS = ("LEFT  (microphone)", "RIGHT (system)")


@dataclass(frozen=True)
class Audio:
    """Deinterleaved audio read from a WAV file."""

    channels: list[list[float]]
    sample_rate: int

    @property
    def duration_seconds(self) -> float:
        if not self.channels or self.sample_rate == 0:
            return 0.0
        return len(self.channels[0]) / self.sample_rate


def read_wav(path: Path) -> Audio:
    """Read a PCM or IEEE-float WAV into per-channel float lists.

    ``wave`` cannot read 32-bit float WAV files, which is exactly what
    nb-capture writes, so the chunks are walked by hand.
    """
    data = path.read_bytes()
    if data[:4] != b"RIFF" or data[8:12] != b"WAVE":
        raise SystemExit(f"{path} is not a RIFF/WAVE file")

    pos = 12
    fmt: tuple[int, int, int, int] | None = None
    payload = b""
    while pos + 8 <= len(data):
        chunk_id = data[pos : pos + 4]
        (size,) = struct.unpack("<I", data[pos + 4 : pos + 8])
        body = data[pos + 8 : pos + 8 + size]
        if chunk_id == b"fmt ":
            tag, channels, rate, _, _, bits = struct.unpack("<HHIIHH", body[:16])
            fmt = (tag, channels, rate, bits)
        elif chunk_id == b"data":
            payload = body
        pos += 8 + size + (size & 1)

    if fmt is None:
        raise SystemExit(f"{path} has no fmt chunk")
    tag, channels, rate, bits = fmt

    if bits == 32 and tag in (3, 0xFFFE):
        count = len(payload) // 4
        flat: tuple[float, ...] = struct.unpack(f"<{count}f", payload[: count * 4])
    elif bits == 16:
        count = len(payload) // 2
        raw = struct.unpack(f"<{count}h", payload[: count * 2])
        flat = tuple(sample / 32768.0 for sample in raw)
    else:
        raise SystemExit(f"unsupported WAV sample format: {bits} bit, tag {tag:#06x}")

    return Audio(
        channels=[list(flat[c::channels]) for c in range(channels)],
        sample_rate=rate,
    )


def transcribe_channel(
    samples: list[float],
    sample_rate: int,
    model: object,
    language: str | None,
) -> list[tuple[float, float, str]]:
    """Return (start, end, text) for one channel."""
    import numpy as np

    audio = np.asarray(samples, dtype=np.float32)
    if sample_rate != 16_000:
        raise SystemExit(f"expected 16 kHz audio (nb-capture's working rate), got {sample_rate} Hz")

    # vad_filter drops the long silences a meeting channel is mostly made of,
    # which is also what stops Whisper inventing speech to fill them.
    segments, _info = model.transcribe(  # type: ignore[attr-defined]
        audio,
        language=language,
        vad_filter=True,
        beam_size=5,
    )
    return [(segment.start, segment.end, segment.text.strip()) for segment in segments]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("wav", type=Path, help="stereo capture produced by nb-capture")
    parser.add_argument(
        "--model",
        default="small",
        help="Whisper model size (tiny, base, small, medium, large-v3). Default: small",
    )
    parser.add_argument(
        "--language",
        default=None,
        help="force a language code such as fr or en; omit to auto-detect",
    )
    parser.add_argument(
        "--extract-seconds",
        type=float,
        default=180.0,
        help="transcribe only the first N seconds (C9 asks for a 3-minute extract)",
    )
    args = parser.parse_args()

    audio = read_wav(args.wav)
    print(f"file        : {args.wav}")
    print(f"channels    : {len(audio.channels)}")
    print(f"sample rate : {audio.sample_rate} Hz")
    print(f"duration    : {audio.duration_seconds:.2f} s")

    limit = int(args.extract_seconds * audio.sample_rate)
    from faster_whisper import WhisperModel

    print(f"\nLoading Whisper '{args.model}' on CPU (int8)...")
    model = WhisperModel(args.model, device="cpu", compute_type="int8")

    for index, channel in enumerate(audio.channels):
        label = CHANNEL_LABELS[index] if index < len(CHANNEL_LABELS) else f"channel {index}"
        extract = channel[:limit]
        peak = max((abs(s) for s in extract), default=0.0)
        print(f"\n=== {label} — peak {peak:.4f} ===")
        if peak < 1e-4:
            print("  (silent: nothing to transcribe)")
            continue

        results = transcribe_channel(extract, audio.sample_rate, model, args.language)
        if not results:
            print("  (no speech detected)")
            continue
        for start, end, text in results:
            print(f"  [{start:7.2f} -> {end:7.2f}] {text}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
