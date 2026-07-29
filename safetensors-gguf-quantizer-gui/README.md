# SafeTensors to GGUF Quantizer GUI

Small Tkinter GUI for converting Hugging Face SafeTensors model folders to GGUF and quantizing them with `llama-cpp-python`.

## What It Does

- Converts a Hugging Face model folder with llama.cpp's `convert_hf_to_gguf.py`.
- Quantizes the converted GGUF through the `llama-cpp-python` low-level `llama_model_quantize` binding.
- Optionally runs multimodal projector conversion with `--mmproj` and quantizes the projector to `Q8_0`.
- Uses only Python stdlib for the GUI. Runtime conversion dependencies are PyPI packages plus the CUDA-compatible `llama-cpp-python` wheel.

## Requirements

- Python 3.10, 3.11, or 3.12 recommended for CUDA wheels.
- A current local `llama.cpp` checkout. The GUI expects to find `convert_hf_to_gguf.py` in that folder.
- A Hugging Face model directory containing `config.json`, tokenizer files, and `.safetensors` shards.

Install `llama-cpp-python` with the wheel matching your CUDA runtime, for example:

```powershell
python -m pip install --upgrade llama-cpp-python --extra-index-url https://abetlen.github.io/llama-cpp-python/whl/cu124
```

Install common conversion dependencies:

```powershell
python -m pip install --upgrade torch transformers safetensors sentencepiece protobuf numpy pyyaml gguf mistral-common
```

If your llama.cpp checkout has a `requirements.txt`, use that as the source of truth for converter dependencies:

```powershell
python -m pip install -r C:\path\to\llama.cpp\requirements.txt
```

## Run

```powershell
python quantize_gui.py
```

or double-click `run_gui.bat`.

## Notes For Vision Models

Enable `Vision/mmproj` for multimodal models. The app runs:

```text
convert_hf_to_gguf.py <model-dir> --mmproj
```

Current llama.cpp support is model-specific. If a vision model is not supported by the converter yet, the GUI will surface the converter error in the log. For supported models, you will get both a text/model GGUF and an `mmproj-*.gguf` file.

