import os
import torch
import transformers.pytorch_utils

# --- MONKEY PATCH FIX ---
# Recreate the missing function for coqui-tts before it imports
if not hasattr(transformers.pytorch_utils, 'isin_mps_friendly'):
    transformers.pytorch_utils.isin_mps_friendly = torch.isin
# ------------------------

import pymupdf as fitz
import re
import numpy as np
from TTS.api import TTS

# --- Configuration ---
PDF_PATH = "example-pdf.pdf"
OUTPUT_DIR = "audiobook_chunks"
# Chunks (200 chars, must match mp3_to_mp4_with_captions.py) longer than this are synthesized
# in smaller pieces and joined into the same WAV, so chunk numbering is unaffected.
SYNTH_MAX_CHARS = 500
# VITS is incredibly fast and efficient for GPU generation
MODEL_NAME = "tts_models/en/ljspeech/vits" 

def extract_text_from_pdf(pdf_path):
    print(f"Extracting text from {pdf_path}...")
    doc = fitz.open(pdf_path)
    text = ""
    for page in doc:
        text += page.get_text() + " "
    
    # Clean up formatting, line breaks, and multiple spaces
    text = text.replace('\n', ' ')
    text = re.sub(r'\s+', ' ', text)
    return text.strip()

def chunk_text(text, max_chars=200):
    """Split text into chunks at sentence boundaries to avoid TTS memory limits."""
    sentences = re.split(r'(?<=[.!?]) +', text)
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

def split_for_synthesis(text, limit):
    """Split text into pieces of at most `limit` chars (sentence ends, then word boundaries).

    The TTS attention memory grows quadratically with input length, and text without
    sentence punctuation (indexes, tables) otherwise reaches the model as one huge input.
    """
    pieces, current = [], ""
    for sentence in re.split(r'(?<=[.!?]) +', text):
        for word in sentence.split():
            if current and len(current) + 1 + len(word) > limit:
                pieces.append(current)
                current = word
            else:
                current = f"{current} {word}".strip()
        if current and len(current) > limit * 0.7:
            pieces.append(current)
            current = ""
    if current:
        pieces.append(current)
    return pieces

def synthesize_to_file(tts, text, file_path, limit=SYNTH_MAX_CHARS):
    """Synthesize `text` into one WAV, in smaller pieces when it is long or the GPU runs out of memory."""
    pieces = split_for_synthesis(text, limit) if len(text) > limit else [text]
    waves = []
    for piece in pieces:
        try:
            waves.append(np.asarray(tts.tts(text=piece), dtype=np.float32))
        except torch.OutOfMemoryError:
            torch.cuda.empty_cache()
            if len(piece) <= 60:
                raise
            print(f"  Out of GPU memory on {len(piece)} chars, retrying in smaller pieces...")
            for sub in split_for_synthesis(piece, len(piece) // 2 + 1):
                waves.append(np.asarray(tts.tts(text=sub), dtype=np.float32))
    if len(waves) > 1:
        pause = np.zeros(int(0.15 * tts.synthesizer.output_sample_rate), dtype=np.float32)
        parts = []
        for w in waves:
            parts += [w, pause]
        waves = [np.concatenate(parts[:-1])]
    tts.synthesizer.save_wav(wav=waves[0], path=file_path)

def main():
    # 1. Set up GPU Acceleration
    device = "cuda" if torch.cuda.is_available() else "cpu"
    print(f"Hardware initialization: Using {device.upper()}")
    
    if device == "cpu":
        print("WARNING: CUDA not detected. Generation will be slow.")

    # 2. Initialize TTS Model
    print(f"Loading TTS model ({MODEL_NAME}) to RTX 3050...")
    tts = TTS(model_name=MODEL_NAME, progress_bar=False).to(device)

    # 3. Process PDF
    full_text = extract_text_from_pdf(PDF_PATH)
    text_chunks = chunk_text(full_text)
    print(f"Text extracted and split into {len(text_chunks)} processable chunks.")

    # 4. Generate Audio
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    
    print("Beginning audio generation...")
    for i, chunk in enumerate(text_chunks):
        if not chunk.strip():
            continue
            
        output_filepath = os.path.join(OUTPUT_DIR, f"chunk_{i:04d}.wav")

        # Resume support: chunks finished in a previous run are skipped.
        if os.path.exists(output_filepath):
            print(f"Skipping [{i+1}/{len(text_chunks)}]: already exists")
            continue

        print(f"Generating [{i+1}/{len(text_chunks)}]: {output_filepath}")
        
        # Write to a temp file first so an interrupted run never leaves a
        # truncated chunk that would be mistaken for a finished one.
        temp_filepath = output_filepath[:-4] + ".part.wav"
        synthesize_to_file(tts, chunk, temp_filepath)
        os.replace(temp_filepath, output_filepath)

    print(f"Success! All audio chunks saved to the '{OUTPUT_DIR}' directory.")

if __name__ == "__main__":
    main()