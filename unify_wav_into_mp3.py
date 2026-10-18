import argparse
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

DEFAULT_INPUT_DIR = "audiobook_chunks"
DEFAULT_OUTPUT = "audiobook.mp3"


def find_wav_files(input_dir: Path) -> list[Path]:
    # Zero-padded names (chunk_0000.wav) make lexical order the correct order.
    return sorted(input_dir.glob("*.wav"), key=lambda p: p.name.lower())


def write_concat_list(files: list[Path], list_path: Path) -> None:
    with open(list_path, "w", encoding="utf-8") as f:
        for wav in files:
            # ffmpeg concat format: single quotes escaped as '\''
            escaped = wav.resolve().as_posix().replace("'", "'\\''")
            f.write(f"file '{escaped}'\n")


def unify(input_dir: Path, output: Path, bitrate: str, quality: int | None) -> None:
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        sys.exit("ERROR: ffmpeg not found on PATH. Install it and try again.")

    if not input_dir.is_dir():
        sys.exit(f"ERROR: input directory not found: {input_dir}")

    files = find_wav_files(input_dir)
    if not files:
        sys.exit(f"ERROR: no .wav files found in {input_dir}")

    print(f"Found {len(files)} WAV files in '{input_dir}'.")
    output.parent.mkdir(parents=True, exist_ok=True)

    fd, list_name = tempfile.mkstemp(suffix=".txt", prefix="concat_")
    os.close(fd)
    list_path = Path(list_name)
    try:
        write_concat_list(files, list_path)

        cmd = [
            ffmpeg, "-y", "-hide_banner", "-loglevel", "error", "-stats",
            "-f", "concat", "-safe", "0", "-i", str(list_path),
            "-vn", "-codec:a", "libmp3lame",
        ]
        if quality is not None:
            cmd += ["-q:a", str(quality)]
        else:
            cmd += ["-b:a", bitrate]
        cmd.append(str(output))

        print(f"Encoding to '{output}'...")
        result = subprocess.run(cmd)
        if result.returncode != 0:
            sys.exit(f"ERROR: ffmpeg failed with exit code {result.returncode}")
    finally:
        list_path.unlink(missing_ok=True)

    size_mb = output.stat().st_size / (1024 * 1024)
    print(f"Success! Wrote {output} ({size_mb:.1f} MB).")


def main() -> None:
    parser = argparse.ArgumentParser(description="Merge all WAV chunks into a single MP3.")
    parser.add_argument("-i", "--input-dir", type=Path, default=Path(DEFAULT_INPUT_DIR))
    parser.add_argument("-o", "--output", type=Path, default=Path(DEFAULT_OUTPUT))
    parser.add_argument("-b", "--bitrate", default="128k", help="CBR bitrate (default: 128k)")
    parser.add_argument("-q", "--quality", type=int, choices=range(0, 10), metavar="0-9",
                        help="VBR quality (0=best, 9=smallest); overrides --bitrate")
    args = parser.parse_args()
    unify(args.input_dir, args.output, args.bitrate, args.quality)


if __name__ == "__main__":
    main()
