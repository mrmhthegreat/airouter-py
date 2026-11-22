"""Example: speak (text → audio) and transcribe (audio → text).

Mirrors ``examples/python/speak.py`` and ``examples/python/transcribe.py``.
"""
from __future__ import annotations

import sys

import airouter


def speak(out_file: str, text: str) -> int:
    c = airouter.open(airouter.Options())
    # Bootstrap TTS providers too — set GEMINI_API_KEY for Gemini TTS,
    # GROQ_API_KEY for Groq Orpheus.
    c.add_free_pool(
        {
            "GROQ_API_KEY": __import__("os").environ.get("GROQ_API_KEY", ""),
            "GEMINI_API_KEY": __import__("os").environ.get("GEMINI_API_KEY", ""),
        },
        categories=["text", "tts"],
    )
    resp = c.speak(airouter.SpeakRequest(text=text, voice="Kore", format="wav"))
    with open(out_file, "wb") as f:
        f.write(resp.audio)
    print(f"wrote {len(resp.audio)} bytes ({resp.content_type}) to {out_file} via {resp.provider}/{resp.model}", file=sys.stderr)
    c.close()
    return 0


def transcribe(path: str) -> int:
    c = airouter.open(airouter.Options())
    c.add_free_pool(
        {
            "GROQ_API_KEY": __import__("os").environ.get("GROQ_API_KEY", ""),
        },
        categories=["text", "stt"],
    )
    with open(path, "rb") as f:
        audio = f.read()
    resp = c.transcribe(airouter.TranscribeRequest(audio=audio, filename=path))
    print(resp.text)
    c.close()
    return 0


def main() -> int:
    if len(sys.argv) < 3:
        print("usage: python audio.py speak out.wav 'Hello world'", file=sys.stderr)
        print("       python audio.py transcribe recording.mp3", file=sys.stderr)
        return 2
    cmd = sys.argv[1]
    if cmd == "speak":
        return speak(sys.argv[2], " ".join(sys.argv[3:]))
    if cmd == "transcribe":
        return transcribe(sys.argv[2])
    print(f"unknown command: {cmd}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
