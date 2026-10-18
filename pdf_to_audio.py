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
from TTS.api import TTS

# --- Configuration ---
PDF_PATH = "example-pdf.pdf"
OUTPUT_DIR = "audiobook_chunks"
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
        print(f"Generating [{i+1}/{len(text_chunks)}]: {output_filepath}")
        
        # Generate and save WAV file
        tts.tts_to_file(text=chunk, file_path=output_filepath)

    print(f"Success! All audio chunks saved to the '{OUTPUT_DIR}' directory.")

if __name__ == "__main__":
    main()