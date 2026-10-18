"""Turn audiobook.mp3 into an MP4 with captions synced to the narration.

Sync strategy: pdf_to_audio.py produced one WAV per text chunk (chunk_NNNN.wav,
NNNN = chunk index). The exact duration of every WAV is read from its header, so
the start/end of each chunk on the unified MP3 timeline is known precisely.
Inside a chunk, the text is split into short caption cues whose durations are
proportional to their character counts.

Requires pymupdf (same text extraction/chunking as pdf_to_audio.py) and ffmpeg.
"""
import argparse
import math
import re
import shutil
import subprocess
import sys
import tempfile
import wave
from pathlib import Path

import pymupdf as fitz

PDF_PATH = "example-pdf.pdf"
CHUNKS_DIR = "audiobook_chunks"
MP3_PATH = "audiobook.mp3"
OUTPUT_MP4 = "audiobook.mp4"
MAX_CHUNK_CHARS = 200  # must match pdf_to_audio.py


# --- Text extraction / chunking (identical to pdf_to_audio.py) ---
def extract_text_from_pdf(pdf_path):
    print(f"Extracting text from {pdf_path}...")
    doc = fitz.open(pdf_path)
    text = ""
    for page in doc:
        text += page.get_text() + " "
    text = text.replace("\n", " ")
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def chunk_text(text, max_chars=MAX_CHUNK_CHARS):
    sentences = re.split(r"(?<=[.!?]) +", text)
    chunks = []
    current_chunk = ""
    for sentence in sentences:
        if len(current_chunk) + len(sentence) <= max_chars:
            current_chunk += sentence + " "
        else:
            if current_chunk:
                chunks.append(current_chunk.strip())
            current_chunk = sentence + " "
    if current_chunk:
        chunks.append(current_chunk.strip())
    return chunks


# --- Caption building ---
def split_evenly(text: str, max_chars: int) -> list[str]:
    """Split over-long text at word boundaries into near-equal parts."""
    words = text.split()
    parts_needed = math.ceil(len(text) / max_chars)
    target = len(text) / parts_needed
    parts, current = [], ""
    for word in words:
        candidate = f"{current} {word}".strip()
        if current and len(candidate) > target and len(parts) < parts_needed - 1:
            parts.append(current)
            current = word
        else:
            current = candidate
    if current:
        parts.append(current)
    return parts


def make_cues(chunk: str, max_chars: int) -> list[str]:
    """Break one TTS chunk into caption-sized pieces, preferring sentence ends."""
    units = []
    for sentence in re.split(r"(?<=[.!?]) +", chunk):
        sentence = sentence.strip()
        if not sentence:
            continue
        units.extend(split_evenly(sentence, max_chars) if len(sentence) > max_chars else [sentence])

    cues = []
    for unit in units:
        if cues and len(cues[-1]) + 1 + len(unit) <= max_chars:
            cues[-1] += " " + unit
        else:
            cues.append(unit)
    return cues


def two_lines(text: str) -> str:
    """Balance a long cue over two lines for readability."""
    if len(text) <= 42 or " " not in text:
        return text
    mid = len(text) / 2
    best = min((m.start() for m in re.finditer(" ", text)), key=lambda i: abs(i - mid))
    return text[:best] + "\n" + text[best + 1:]


def wav_duration(path: Path) -> float:
    with wave.open(str(path), "rb") as w:
        return w.getnframes() / w.getframerate()


def media_duration(path: Path) -> float:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=nw=1:nk=1", str(path)],
        capture_output=True, text=True, check=True,
    ).stdout
    return float(out.strip())


def srt_time(t: float) -> str:
    ms = round(t * 1000)
    h, ms = divmod(ms, 3_600_000)
    m, ms = divmod(ms, 60_000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def build_cues(pdf_path: Path, chunks_dir: Path, mp3_path: Path, max_chars: int):
    chunks = chunk_text(extract_text_from_pdf(str(pdf_path)))
    print(f"{len(chunks)} text chunks.")

    raw = []  # (start, end, text) on the WAV timeline
    t = 0.0
    missing = 0
    for i, chunk in enumerate(chunks):
        wav_path = chunks_dir / f"chunk_{i:04d}.wav"
        if not chunk.strip():
            continue
        if not wav_path.exists():
            missing += 1
            continue
        dur = wav_duration(wav_path)
        cues = make_cues(chunk, max_chars)
        total_chars = sum(len(c) for c in cues)
        pos = t
        for cue in cues:
            cue_dur = dur * len(cue) / total_chars
            raw.append((pos, pos + cue_dur, cue))
            pos += cue_dur
        t += dur
    if missing:
        print(f"WARNING: {missing} chunk WAV file(s) missing; their text is skipped.")

    # Absorb encoder padding/rounding so the last cue lines up with the MP3 end.
    mp3_dur = media_duration(mp3_path)
    drift = mp3_dur - t
    print(f"WAV total {t:.2f}s, MP3 {mp3_dur:.2f}s (drift {drift:+.3f}s).")
    scale = mp3_dur / t if t > 0 and abs(drift) < 0.01 * t else 1.0
    if scale == 1.0 and abs(drift) >= 0.01 * t:
        print("WARNING: MP3 length differs >1% from the WAVs; is audiobook.mp3 up to date?")
    return [(s * scale, e * scale, txt) for s, e, txt in raw]


def write_srt(cues, path: Path) -> None:
    with open(path, "w", encoding="utf-8") as f:
        for n, (start, end, text) in enumerate(cues, 1):
            f.write(f"{n}\n{srt_time(start)} --> {srt_time(end)}\n{two_lines(text)}\n\n")


# --- Video ---
def render_mp4(mp3_path: Path, srt_path: Path, out: Path, mode: str,
               size: str, fps: int, font_size: int) -> None:
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        sys.exit("ERROR: ffmpeg not found on PATH.")

    base = [ffmpeg, "-y", "-hide_banner", "-loglevel", "error", "-stats", "-stats_period", "30",
            "-f", "lavfi", "-i", f"color=c=black:s={size}:r={fps}", "-i", str(mp3_path.resolve())]

    if mode == "burn":
        # The subtitles filter chokes on Windows drive-letter paths, so run
        # ffmpeg from the SRT's directory and reference it by bare filename.
        style = (f"FontName=Arial,FontSize={font_size},PrimaryColour=&H00FFFFFF,"
                 "OutlineColour=&H00000000,BorderStyle=1,Outline=2,Alignment=2,MarginV=60")
        cmd = base + [
            "-vf", f"subtitles={srt_path.name}:force_style='{style}'",
            "-c:v", "libx264", "-preset", "veryfast", "-tune", "stillimage", "-pix_fmt", "yuv420p",
            "-c:a", "copy", "-shortest", "-movflags", "+faststart", str(out.resolve())]
        cwd = srt_path.parent
    else:
        cmd = base + [
            "-i", str(srt_path.resolve()),
            "-map", "0:v", "-map", "1:a", "-map", "2:s",
            "-c:v", "libx264", "-preset", "veryfast", "-tune", "stillimage", "-pix_fmt", "yuv420p",
            "-c:a", "copy", "-c:s", "mov_text", "-metadata:s:s:0", "language=eng",
            "-disposition:s:0", "default", "-shortest", "-movflags", "+faststart", str(out.resolve())]
        cwd = None

    print(f"Rendering '{out}' ({mode} captions)... this can take a while for long audio.")
    if subprocess.run(cmd, cwd=cwd).returncode != 0:
        sys.exit("ERROR: ffmpeg failed.")


def main() -> None:
    p = argparse.ArgumentParser(description="Convert audiobook.mp3 to a captioned MP4.")
    p.add_argument("--pdf", type=Path, default=Path(PDF_PATH))
    p.add_argument("--chunks-dir", type=Path, default=Path(CHUNKS_DIR))
    p.add_argument("--mp3", type=Path, default=Path(MP3_PATH))
    p.add_argument("-o", "--output", type=Path, default=Path(OUTPUT_MP4))
    p.add_argument("--mode", choices=["burn", "soft"], default="burn",
                   help="burn: captions drawn into the video (always visible, slower); "
                        "soft: selectable subtitle track (fast)")
    p.add_argument("--size", default="1280x720")
    p.add_argument("--fps", type=int, default=4, help="video frame rate (caption timing granularity)")
    p.add_argument("--font-size", type=int, default=24)
    p.add_argument("--max-cue-chars", type=int, default=84)
    p.add_argument("--srt-only", action="store_true", help="only write the .srt file")
    args = p.parse_args()

    for path in (args.pdf, args.mp3):
        if not path.is_file():
            sys.exit(f"ERROR: file not found: {path}")
    if not args.chunks_dir.is_dir():
        sys.exit(f"ERROR: directory not found: {args.chunks_dir}")

    cues = build_cues(args.pdf, args.chunks_dir, args.mp3, args.max_cue_chars)
    srt_final = args.output.with_suffix(".srt")
    write_srt(cues, srt_final)
    print(f"Wrote {len(cues)} captions to {srt_final}")
    if args.srt_only:
        return

    with tempfile.TemporaryDirectory(prefix="caps_") as tmp:
        tmp_srt = Path(tmp) / "captions.srt"
        shutil.copyfile(srt_final, tmp_srt)
        render_mp4(args.mp3, tmp_srt, args.output, args.mode, args.size, args.fps, args.font_size)

    print(f"Success! Wrote {args.output} ({args.output.stat().st_size / 1024 / 1024:.0f} MB).")


if __name__ == "__main__":
    main()
