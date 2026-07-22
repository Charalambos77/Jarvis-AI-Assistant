"""
record_samples.py

Utility to capture positive/negative wake-word audio samples for training.
Requires `pyaudio` (already in repo requirements).

Usage examples:
  python scripts/record_samples.py --mode positive --label jarvis --count 200 --duration 1.2
  python scripts/record_samples.py --mode negative --count 300 --duration 1.5

This saves WAV files under `data/positive/<label>/` or `data/negative/`.
"""

import argparse
import os
import time
import wave
import math
from pathlib import Path

import pyaudio

SAMPLE_RATE = 16000
CHANNELS = 1
FORMAT = pyaudio.paInt16


def record_wave(path: Path, duration: float):
    pa = pyaudio.PyAudio()
    frames = []
    stream = pa.open(format=FORMAT,
                     channels=CHANNELS,
                     rate=SAMPLE_RATE,
                     input=True,
                     frames_per_buffer=1024)
    try:
        num_frames = int(SAMPLE_RATE / 1024 * duration)
        for _ in range(num_frames):
            data = stream.read(1024, exception_on_overflow=False)
            frames.append(data)
    finally:
        stream.stop_stream()
        stream.close()
        pa.terminate()

    with wave.open(str(path), 'wb') as wf:
        wf.setnchannels(CHANNELS)
        wf.setsampwidth(pyaudio.PyAudio().get_sample_size(FORMAT))
        wf.setframerate(SAMPLE_RATE)
        wf.writeframes(b''.join(frames))


def ensure_dir(p: Path):
    if not p.exists():
        p.mkdir(parents=True, exist_ok=True)


def main():
    parser = argparse.ArgumentParser(description="Record wake-word samples")
    parser.add_argument('--mode', choices=['positive', 'negative'], required=True)
    parser.add_argument('--label', type=str, default='default', help='Positive label (e.g. jarvis)')
    parser.add_argument('--count', type=int, default=100)
    parser.add_argument('--duration', type=float, default=1.2, help='Seconds per sample')
    parser.add_argument('--out', type=str, default='data', help='Output base folder')
    parser.add_argument('--delay', type=float, default=0.8, help='Seconds between samples')
    args = parser.parse_args()

    base = Path(args.out)
    if args.mode == 'positive':
        dest = base / 'positive' / args.label
    else:
        dest = base / 'negative'
    ensure_dir(dest)

    print(f"Recording {args.count} {args.mode} samples to: {dest}\n")
    print("Make sure your mic is set up. Speak naturally. Press Ctrl+C to stop.")

    try:
        for i in range(args.count):
            idx = i + 1
            fname = dest / f"sample_{idx:04d}.wav"
            print(f"Sample {idx}/{args.count} — prepare...")
            # brief countdown
            for s in range(3, 0, -1):
                print(f"  recording in {s}...", end='\r')
                time.sleep(1)
            print('  recording now...           ')
            try:
                record_wave(fname, args.duration)
                print(f"  saved: {fname}")
            except Exception as e:
                print(f"  failed to record sample {idx}: {e}")

            if idx < args.count:
                time.sleep(args.delay)
    except KeyboardInterrupt:
        print('\nRecording interrupted by user.')


if __name__ == '__main__':
    main()
