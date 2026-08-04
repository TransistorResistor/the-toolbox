# Qwen3-VL PDF Metadata Extractor

A basic Tkinter desktop application that:

- selects a folder of PDFs;
- randomly selects up to 10 files;
- renders each PDF's first and last pages;
- sends both page images to a Qwen3-VL GGUF model;
- extracts fields defined by the user;
- performs Unicode-aware quality checks on the PDF text layer;
- compares model output with the selected-page text layer where possible;
- presents every result, including empty or garbled text-layer warnings;
- exports CSV and JSON.

## Required files

You need two matching files from the same Qwen3-VL GGUF conversion:

1. The language model GGUF, for example a Q4_K_M quantisation.
2. The multimodal projector GGUF, usually containing `mmproj` in its filename.

Do not mix a model GGUF and projector from different conversions or model variants.

## Python

Python 3.10–3.12 is the safest range for the published accelerated wheels.

Create and activate a virtual environment, then install the dependencies.

### CPU

```bash
python -m venv .venv
# Windows:
.venv\Scripts\activate
# macOS/Linux:
source .venv/bin/activate

python -m pip install --upgrade pip
python -m pip install -r requirements.txt \
  --extra-index-url https://abetlen.github.io/llama-cpp-python/whl/cpu
```

### NVIDIA CUDA

Choose the wheel index matching your installed CUDA runtime. For example, CUDA 12.4:

```bash
python -m pip install --upgrade pip
python -m pip install -r requirements.txt \
  --extra-index-url https://abetlen.github.io/llama-cpp-python/whl/cu124
```

Other published 0.3.34 wheel channels include `cu122`, `cu123`, `cu125`,
`cu130`, and `cu132`.

### Apple Silicon / Metal

```bash
python -m pip install --upgrade pip
python -m pip install -r requirements.txt \
  --extra-index-url https://abetlen.github.io/llama-cpp-python/whl/metal
```

## Run

```bash
python qwen_vl_pdf_metadata_gui.py
```

In the GUI:

1. Select a folder containing PDFs.
2. Select the Qwen3-VL model GGUF.
3. Select its matching mmproj GGUF.
4. Enter one field per line.
5. Press **Process up to 10 PDFs**.
6. Double-click a result to inspect full extraction and QC details.

The random seed is optional. Reusing the same seed with an unchanged folder
produces the same sample.

## QC interpretation

- `OK`: selected-page text extraction looks broadly usable.
- `WARNING`: some suspicious properties were found, but text remains useful.
- `EMPTY`: no usable text was extracted.
- `GARBLED/EMPTY`: the text layer appears severely malformed or nearly empty.
- `ERROR`: the PDF could not be processed.

The checks rely on Unicode character properties rather than ASCII-only tests.
Accented Spanish, Cyrillic, Arabic, CJK and other scripts are therefore not
treated as garbled solely because they are non-Latin.

Field matching is advisory. A field may be correctly read from the image but
fail to match the PDF text layer due to broken encodings, ligatures or scans.

## Notes

- The program deliberately processes PDFs sequentially. Running several
  multimodal requests concurrently can multiply memory use.
- Increase context size when extracting many long fields.
- Higher DPI can help small print, but increases vision-token use and processing.
- The app enforces `llama-cpp-python==0.3.34` at runtime.
