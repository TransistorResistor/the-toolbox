#!/usr/bin/env python3
"""
Bilingual PDF metadata extractor using Qwen3-VL GGUF and llama-cpp-python 0.3.34.

Features
--------
- Basic Tkinter GUI.
- Select a folder containing PDFs.
- Randomly samples up to 10 PDFs.
- Renders first and last pages for Qwen3-VL.
- Extracts user-defined fields into JSON.
- Performs Unicode-aware QC on the PDF text layer.
- Cross-checks extracted values against first/last-page text where possible.
- Displays all results, including PDFs with empty or garbled text layers.
- Exports results to CSV or JSON.

Expected model files
--------------------
1. Qwen3-VL language-model GGUF.
2. Matching multimodal projector (mmproj) GGUF.

The program targets llama-cpp-python==0.3.34 and uses MTMDChatHandler.
"""

from __future__ import annotations

import base64
import csv
import io
import json
import os
import random
import re
import threading
import traceback
import unicodedata
from dataclasses import asdict, dataclass, field
from difflib import SequenceMatcher
from pathlib import Path
from queue import Empty, Queue
from typing import Any, Iterable

import fitz  # PyMuPDF
from PIL import Image
import tkinter as tk
from tkinter import filedialog, messagebox, ttk


MAX_PDFS = 10
DEFAULT_DPI = 180
DEFAULT_CONTEXT = 8192
DEFAULT_MAX_TOKENS = 1400

# These controls are treated as suspicious. Normal whitespace is allowed.
ALLOWED_CONTROLS = {"\n", "\r", "\t", "\f"}


@dataclass
class TextLayerQC:
    status: str
    warnings: list[str] = field(default_factory=list)
    character_count: int = 0
    non_whitespace_count: int = 0
    alphanumeric_ratio: float = 0.0
    control_ratio: float = 0.0
    replacement_ratio: float = 0.0
    unique_ratio: float = 0.0
    first_page_characters: int = 0
    last_page_characters: int = 0


@dataclass
class FieldQC:
    field: str
    present: bool
    found_in_text_layer: bool | None
    match_score: float | None
    note: str = ""


@dataclass
class ExtractionRecord:
    filename: str
    path: str
    page_count: int
    selected_pages: list[int]
    text_layer_qc: TextLayerQC
    extracted_fields: dict[str, Any]
    field_qc: list[FieldQC]
    model_raw_response: str = ""
    error: str = ""


def clean_field_names(raw: str) -> list[str]:
    """Parse one field per line or comma-separated fields, preserving order."""
    chunks = re.split(r"[\n,]+", raw)
    fields: list[str] = []
    seen: set[str] = set()
    for chunk in chunks:
        name = chunk.strip()
        if name and name.casefold() not in seen:
            fields.append(name)
            seen.add(name.casefold())
    return fields


def normalise_for_match(value: str) -> str:
    """Unicode-friendly normalisation for approximate text-layer matching."""
    value = unicodedata.normalize("NFKC", value).casefold()
    value = "".join(
        ch if (ch.isalnum() or ch.isspace()) else " "
        for ch in value
    )
    return " ".join(value.split())


def iter_scalar_strings(value: Any) -> Iterable[str]:
    if value is None:
        return
    if isinstance(value, str):
        if value.strip():
            yield value.strip()
    elif isinstance(value, (int, float, bool)):
        yield str(value)
    elif isinstance(value, list):
        for item in value:
            yield from iter_scalar_strings(item)
    elif isinstance(value, dict):
        # Common structured extraction shape: {"value": ..., "page": ..., ...}
        if "value" in value:
            yield from iter_scalar_strings(value["value"])
        else:
            for item in value.values():
                yield from iter_scalar_strings(item)


def text_layer_qc(first_text: str, last_text: str) -> TextLayerQC:
    """
    Assess whether text extraction looks usable without assuming Latin script.

    str.isalnum() is Unicode-aware, so Arabic, Cyrillic, CJK, accented Spanish,
    and other writing systems count as legitimate alphanumeric content.
    """
    combined = first_text
    if last_text and last_text != first_text:
        combined += "\n" + last_text

    char_count = len(combined)
    compact = [ch for ch in combined if not ch.isspace()]
    non_ws = len(compact)

    if non_ws == 0:
        return TextLayerQC(
            status="EMPTY",
            warnings=["No usable text was extracted from the selected PDF pages."],
            character_count=char_count,
            non_whitespace_count=0,
            first_page_characters=len(first_text),
            last_page_characters=len(last_text),
        )

    alnum = sum(ch.isalnum() for ch in compact)
    controls = sum(
        unicodedata.category(ch) == "Cc" and ch not in ALLOWED_CONTROLS
        for ch in combined
    )
    replacements = sum(ch in {"\ufffd", "\x00"} for ch in combined)
    unique_ratio = len(set(compact)) / max(1, non_ws)

    alnum_ratio = alnum / non_ws
    control_ratio = controls / max(1, char_count)
    replacement_ratio = replacements / max(1, non_ws)

    warnings: list[str] = []

    if non_ws < 40:
        warnings.append("Text layer is close to empty on the selected pages.")
    if replacement_ratio > 0.005:
        warnings.append("Text contains replacement or NUL characters.")
    if control_ratio > 0.002:
        warnings.append("Text contains an unusual number of control characters.")
    if alnum_ratio < 0.30 and non_ws >= 40:
        warnings.append(
            "Low Unicode alphanumeric ratio; the text layer may be fragmented or garbled."
        )
    if unique_ratio < 0.015 and non_ws > 250:
        warnings.append("Very low character diversity suggests repeated or corrupt text.")

    # Detect very long repeated-character runs without penalising normal scripts.
    if re.search(r"(.)\1{18,}", combined, flags=re.DOTALL):
        warnings.append("Long repeated-character runs suggest a malformed text layer.")

    # Detect excessive isolated one-character fragments, common in broken PDF extraction.
    tokens = re.findall(r"\S+", combined, flags=re.UNICODE)
    if len(tokens) >= 30:
        singletons = sum(len(token) == 1 for token in tokens)
        if singletons / len(tokens) > 0.72:
            warnings.append("Most text tokens are isolated characters; extraction may be garbled.")

    severe = (
        non_ws < 15
        or replacement_ratio > 0.03
        or control_ratio > 0.02
        or (alnum_ratio < 0.12 and non_ws >= 40)
    )

    if severe:
        status = "GARBLED/EMPTY"
    elif warnings:
        status = "WARNING"
    else:
        status = "OK"

    return TextLayerQC(
        status=status,
        warnings=warnings,
        character_count=char_count,
        non_whitespace_count=non_ws,
        alphanumeric_ratio=round(alnum_ratio, 4),
        control_ratio=round(control_ratio, 4),
        replacement_ratio=round(replacement_ratio, 4),
        unique_ratio=round(unique_ratio, 4),
        first_page_characters=len(first_text),
        last_page_characters=len(last_text),
    )


def render_page_to_data_uri(page: fitz.Page, dpi: int) -> str:
    scale = dpi / 72.0
    pix = page.get_pixmap(
        matrix=fitz.Matrix(scale, scale),
        alpha=False,
        colorspace=fitz.csRGB,
    )
    image = Image.open(io.BytesIO(pix.tobytes("png"))).convert("RGB")

    # Bound extreme page dimensions while retaining enough detail for OCR.
    max_side = 2200
    if max(image.size) > max_side:
        image.thumbnail((max_side, max_side), Image.Resampling.LANCZOS)

    buffer = io.BytesIO()
    image.save(buffer, format="JPEG", quality=90, optimize=True)
    encoded = base64.b64encode(buffer.getvalue()).decode("ascii")
    return f"data:image/jpeg;base64,{encoded}"


def extract_json_object(raw: str) -> dict[str, Any]:
    """Parse JSON, tolerating markdown fences or limited surrounding prose."""
    text = raw.strip()

    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.IGNORECASE)
        text = re.sub(r"\s*```$", "", text)

    try:
        parsed = json.loads(text)
        if isinstance(parsed, dict):
            return parsed
        raise ValueError("Model returned JSON that is not an object.")
    except json.JSONDecodeError:
        start = text.find("{")
        end = text.rfind("}")
        if start >= 0 and end > start:
            parsed = json.loads(text[start : end + 1])
            if isinstance(parsed, dict):
                return parsed
        raise


def build_prompt(fields: list[str], page_numbers: list[int], text_qc: TextLayerQC) -> str:
    schema_lines = "\n".join(f'- "{field}"' for field in fields)
    pages = ", ".join(str(page) for page in page_numbers)

    return f"""
You are performing conservative bibliographic/document metadata extraction.

You are given images of PDF pages {pages}. They are the first and last pages
of the document, or just one page if the PDF has only one page.

Extract these user-requested fields:
{schema_lines}

Rules:
1. Treat this as transcription and metadata extraction, not creative writing.
2. Preserve the language and wording printed in the document.
3. Do not translate, paraphrase, modernise, or silently correct extracted text.
4. Bilingual or multilingual values must remain distinct. When a field has
   multiple language versions, return an object keyed by BCP-47-style language
   code where reasonably identifiable, for example:
   {{"es": "...", "en": "..."}}.
5. Do not invent a translation or a field that is not visible.
6. Return null when a requested field is absent or unreadable.
7. Ignore running headers, footers, reference-list entries and unrelated text.
8. For names, identifiers, dates and titles, preserve accents and punctuation.
9. If uncertain, use an object:
   {{"value": <best transcription or null>, "uncertain": true,
     "reason": "<brief reason>", "page": <page number or null>}}
10. Otherwise return the plain extracted value, list, or language-keyed object.
11. Return one valid JSON object only, with exactly the requested field names
    as top-level keys. Do not include commentary or markdown.

PDF text-layer QC before vision extraction: {text_qc.status}.
This is advisory only. Extract from the page images even when the text layer is
empty or garbled.
""".strip()


def field_qc_against_text(
    fields: dict[str, Any],
    source_text: str,
    text_status: str,
) -> list[FieldQC]:
    """
    Check whether model-extracted scalar values appear in PDF text extraction.

    This is a QC signal only. A negative result is not proof of model error:
    scans, encoding problems, line breaks and ligatures can all cause mismatch.
    """
    source_norm = normalise_for_match(source_text)
    output: list[FieldQC] = []

    text_usable = text_status not in {"EMPTY", "GARBLED/EMPTY"} and bool(source_norm)

    for name, value in fields.items():
        values = list(iter_scalar_strings(value))
        present = bool(values)

        if not present:
            output.append(
                FieldQC(
                    field=name,
                    present=False,
                    found_in_text_layer=None,
                    match_score=None,
                    note="No value extracted.",
                )
            )
            continue

        if not text_usable:
            output.append(
                FieldQC(
                    field=name,
                    present=True,
                    found_in_text_layer=None,
                    match_score=None,
                    note="Text layer unsuitable for comparison.",
                )
            )
            continue

        best_score = 0.0
        all_found = True

        for candidate in values:
            candidate_norm = normalise_for_match(candidate)
            if not candidate_norm:
                continue

            if candidate_norm in source_norm:
                score = 1.0
            else:
                # Compare against windows around candidate length.
                words = source_norm.split()
                candidate_words = candidate_norm.split()
                n = len(candidate_words)
                score = 0.0

                if n:
                    # Keep runtime bounded for large text layers.
                    step = max(1, n // 3)
                    lower = max(1, n - max(2, n // 4))
                    upper = n + max(2, n // 4)
                    for size in range(lower, upper + 1, step):
                        for idx in range(0, max(1, len(words) - size + 1), max(1, size // 3)):
                            window = " ".join(words[idx : idx + size])
                            score = max(
                                score,
                                SequenceMatcher(
                                    None, candidate_norm, window, autojunk=False
                                ).ratio(),
                            )
                            if score >= 0.93:
                                break
                        if score >= 0.93:
                            break

            best_score = max(best_score, score)
            # Short codes/numbers need stricter matching than long prose.
            threshold = 0.94 if len(candidate_norm) < 12 else 0.78
            if score < threshold:
                all_found = False

        output.append(
            FieldQC(
                field=name,
                present=True,
                found_in_text_layer=all_found,
                match_score=round(best_score, 3),
                note=(
                    "All scalar values approximately matched the selected-page text layer."
                    if all_found
                    else "One or more values were not confidently matched in the text layer."
                ),
            )
        )

    return output


class QwenVLExtractor:
    def __init__(
        self,
        model_path: str,
        mmproj_path: str,
        n_ctx: int,
        n_gpu_layers: int,
        n_threads: int,
        max_tokens: int,
    ) -> None:
        try:
            import llama_cpp
            from llama_cpp import Llama
            from llama_cpp.llama_chat_format import MTMDChatHandler
        except ImportError as exc:
            raise RuntimeError(
                "llama-cpp-python is not installed. Install llama-cpp-python==0.3.34."
            ) from exc

        installed = getattr(llama_cpp, "__version__", "unknown")
        if installed != "0.3.34":
            raise RuntimeError(
                f"This program targets llama-cpp-python 0.3.34, but found {installed}. "
                "Install the pinned version before running."
            )

        if not Path(model_path).is_file():
            raise FileNotFoundError(f"Model GGUF not found: {model_path}")
        if not Path(mmproj_path).is_file():
            raise FileNotFoundError(f"mmproj GGUF not found: {mmproj_path}")

        # 0.3.34 generic libmtmd multimodal handler.
        self.chat_handler = MTMDChatHandler(
            clip_model_path=mmproj_path,
            verbose=False,
        )

        self.llm = Llama(
            model_path=model_path,
            chat_handler=self.chat_handler,
            n_ctx=n_ctx,
            n_gpu_layers=n_gpu_layers,
            n_threads=n_threads,
            logits_all=False,
            verbose=False,
        )
        self.max_tokens = max_tokens

    def close(self) -> None:
        # Explicitly close the MTMD handler before the model. This avoids stale
        # multimodal contexts in builds where normal teardown is incomplete.
        handler = getattr(self, "chat_handler", None)
        stack = getattr(handler, "_exit_stack", None)
        if stack is not None:
            try:
                stack.close()
            except Exception:
                pass

        llm = getattr(self, "llm", None)
        if llm is not None:
            try:
                llm.close()
            except Exception:
                pass

    def extract(
        self,
        prompt: str,
        page_images: list[tuple[int, str]],
        field_names: list[str],
    ) -> tuple[dict[str, Any], str]:
        content: list[dict[str, Any]] = [{"type": "text", "text": prompt}]
        for page_number, data_uri in page_images:
            content.append(
                {
                    "type": "text",
                    "text": f"PDF page {page_number}:",
                }
            )
            content.append(
                {
                    "type": "image_url",
                    "image_url": {"url": data_uri},
                }
            )

        # JSON mode is supported by the llama-cpp-python chat-completion API.
        response = self.llm.create_chat_completion(
            messages=[
                {
                    "role": "system",
                    "content": (
                        "You are a precise multilingual document metadata "
                        "transcription system. Output JSON only."
                    ),
                },
                {"role": "user", "content": content},
            ],
            temperature=0.0,
            top_p=0.9,
            max_tokens=self.max_tokens,
            response_format={"type": "json_object"},
        )

        choice = response["choices"][0]
        raw = choice.get("message", {}).get("content")
        if raw is None:
            raw = choice.get("text", "")
        raw = str(raw or "")

        parsed = extract_json_object(raw)

        # Enforce exact user-defined top-level field set.
        ordered = {field: parsed.get(field) for field in field_names}
        return ordered, raw


class MetadataExtractorGUI(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("Qwen3-VL PDF Metadata Extractor")
        self.geometry("1240x820")
        self.minsize(960, 650)

        self.folder_var = tk.StringVar()
        self.model_var = tk.StringVar()
        self.mmproj_var = tk.StringVar()
        self.dpi_var = tk.IntVar(value=DEFAULT_DPI)
        self.ctx_var = tk.IntVar(value=DEFAULT_CONTEXT)
        self.gpu_layers_var = tk.IntVar(value=-1)
        self.threads_var = tk.IntVar(value=max(1, (os.cpu_count() or 4) - 1))
        self.max_tokens_var = tk.IntVar(value=DEFAULT_MAX_TOKENS)
        self.seed_var = tk.StringVar()
        self.status_var = tk.StringVar(value="Ready.")
        self.progress_var = tk.DoubleVar(value=0)

        self.records: list[ExtractionRecord] = []
        self.events: Queue[tuple[str, Any]] = Queue()
        self.worker: threading.Thread | None = None

        self._build_ui()
        self.after(100, self._poll_events)

    def _build_ui(self) -> None:
        outer = ttk.Frame(self, padding=10)
        outer.pack(fill="both", expand=True)

        config = ttk.LabelFrame(outer, text="Input and model", padding=8)
        config.pack(fill="x")

        self._path_row(config, 0, "PDF folder", self.folder_var, self._choose_folder)
        self._path_row(config, 1, "Model GGUF", self.model_var, self._choose_model)
        self._path_row(config, 2, "mmproj GGUF", self.mmproj_var, self._choose_mmproj)

        settings = ttk.Frame(config)
        settings.grid(row=3, column=0, columnspan=3, sticky="ew", pady=(8, 0))

        numeric_settings = [
            ("DPI", self.dpi_var, 5),
            ("Context", self.ctx_var, 8),
            ("GPU layers", self.gpu_layers_var, 7),
            ("CPU threads", self.threads_var, 6),
            ("Max output tokens", self.max_tokens_var, 7),
        ]
        for idx, (label, variable, width) in enumerate(numeric_settings):
            ttk.Label(settings, text=label).grid(row=0, column=idx * 2, padx=(0, 4))
            ttk.Entry(settings, textvariable=variable, width=width).grid(
                row=0, column=idx * 2 + 1, padx=(0, 12)
            )

        ttk.Label(settings, text="Random seed (optional)").grid(
            row=0, column=10, padx=(0, 4)
        )
        ttk.Entry(settings, textvariable=self.seed_var, width=12).grid(row=0, column=11)

        fields_box = ttk.LabelFrame(outer, text="Fields to extract", padding=8)
        fields_box.pack(fill="x", pady=(10, 0))

        ttk.Label(
            fields_box,
            text=(
                "Enter one field per line or separate fields with commas. "
                "Examples: title, abstract, authors, DOI, publication year, keywords"
            ),
        ).pack(anchor="w")

        self.fields_text = tk.Text(fields_box, height=5, wrap="word")
        self.fields_text.pack(fill="x", pady=(5, 0))
        self.fields_text.insert(
            "1.0",
            "title\nabstract\nauthors\naffiliations\nkeywords\nDOI\npublication year",
        )

        actions = ttk.Frame(outer)
        actions.pack(fill="x", pady=10)

        self.run_button = ttk.Button(actions, text="Process up to 10 PDFs", command=self._start)
        self.run_button.pack(side="left")
        ttk.Button(actions, text="Export CSV", command=self._export_csv).pack(
            side="left", padx=(8, 0)
        )
        ttk.Button(actions, text="Export JSON", command=self._export_json).pack(
            side="left", padx=(8, 0)
        )
        ttk.Button(actions, text="Clear", command=self._clear).pack(
            side="left", padx=(8, 0)
        )

        ttk.Progressbar(
            actions, variable=self.progress_var, maximum=100, length=260
        ).pack(side="right")

        results_box = ttk.LabelFrame(outer, text="Extraction results", padding=5)
        results_box.pack(fill="both", expand=True)

        self.tree = ttk.Treeview(results_box, show="headings")
        y_scroll = ttk.Scrollbar(results_box, orient="vertical", command=self.tree.yview)
        x_scroll = ttk.Scrollbar(results_box, orient="horizontal", command=self.tree.xview)
        self.tree.configure(yscrollcommand=y_scroll.set, xscrollcommand=x_scroll.set)

        self.tree.grid(row=0, column=0, sticky="nsew")
        y_scroll.grid(row=0, column=1, sticky="ns")
        x_scroll.grid(row=1, column=0, sticky="ew")
        results_box.rowconfigure(0, weight=1)
        results_box.columnconfigure(0, weight=1)

        self.tree.bind("<Double-1>", self._show_record)

        status = ttk.Frame(outer)
        status.pack(fill="x", pady=(8, 0))
        ttk.Label(status, textvariable=self.status_var).pack(side="left")

    def _path_row(
        self,
        parent: ttk.Widget,
        row: int,
        label: str,
        variable: tk.StringVar,
        command: Any,
    ) -> None:
        ttk.Label(parent, text=label, width=13).grid(row=row, column=0, sticky="w", pady=3)
        ttk.Entry(parent, textvariable=variable).grid(
            row=row, column=1, sticky="ew", padx=(0, 6), pady=3
        )
        ttk.Button(parent, text="Browse…", command=command).grid(row=row, column=2, pady=3)
        parent.columnconfigure(1, weight=1)

    def _choose_folder(self) -> None:
        path = filedialog.askdirectory(title="Select folder containing PDFs")
        if path:
            self.folder_var.set(path)

    def _choose_model(self) -> None:
        path = filedialog.askopenfilename(
            title="Select Qwen3-VL model GGUF",
            filetypes=[("GGUF models", "*.gguf"), ("All files", "*.*")],
        )
        if path:
            self.model_var.set(path)

    def _choose_mmproj(self) -> None:
        path = filedialog.askopenfilename(
            title="Select matching mmproj GGUF",
            filetypes=[("GGUF projectors", "*.gguf"), ("All files", "*.*")],
        )
        if path:
            self.mmproj_var.set(path)

    def _validate(self) -> tuple[Path, list[str]]:
        folder = Path(self.folder_var.get()).expanduser()
        if not folder.is_dir():
            raise ValueError("Select a valid PDF folder.")

        if not Path(self.model_var.get()).is_file():
            raise ValueError("Select a valid model GGUF.")
        if not Path(self.mmproj_var.get()).is_file():
            raise ValueError("Select a valid mmproj GGUF.")

        fields = clean_field_names(self.fields_text.get("1.0", "end"))
        if not fields:
            raise ValueError("Enter at least one field to extract.")

        if self.dpi_var.get() < 72 or self.dpi_var.get() > 400:
            raise ValueError("DPI should be between 72 and 400.")
        if self.ctx_var.get() < 2048:
            raise ValueError("Context size should be at least 2048.")
        if self.max_tokens_var.get() < 100:
            raise ValueError("Max output tokens should be at least 100.")

        return folder, fields

    def _start(self) -> None:
        if self.worker and self.worker.is_alive():
            return

        try:
            folder, fields = self._validate()
        except Exception as exc:
            messagebox.showerror("Configuration error", str(exc))
            return

        pdfs = sorted(folder.glob("*.pdf"))
        if not pdfs:
            messagebox.showwarning("No PDFs", "The selected folder contains no PDF files.")
            return

        seed_text = self.seed_var.get().strip()
        rng = random.Random(seed_text if seed_text else None)
        selected = rng.sample(pdfs, min(MAX_PDFS, len(pdfs)))

        self.records.clear()
        self._configure_tree(fields)
        self.progress_var.set(0)
        self.run_button.configure(state="disabled")
        self.status_var.set(
            f"Loading model and preparing {len(selected)} randomly selected PDF(s)…"
        )

        args = {
            "pdfs": selected,
            "fields": fields,
            "model_path": self.model_var.get(),
            "mmproj_path": self.mmproj_var.get(),
            "dpi": self.dpi_var.get(),
            "n_ctx": self.ctx_var.get(),
            "n_gpu_layers": self.gpu_layers_var.get(),
            "n_threads": self.threads_var.get(),
            "max_tokens": self.max_tokens_var.get(),
        }
        self.worker = threading.Thread(target=self._worker, kwargs=args, daemon=True)
        self.worker.start()

    def _worker(
        self,
        pdfs: list[Path],
        fields: list[str],
        model_path: str,
        mmproj_path: str,
        dpi: int,
        n_ctx: int,
        n_gpu_layers: int,
        n_threads: int,
        max_tokens: int,
    ) -> None:
        extractor: QwenVLExtractor | None = None
        try:
            self.events.put(("status", "Loading Qwen3-VL model…"))
            extractor = QwenVLExtractor(
                model_path=model_path,
                mmproj_path=mmproj_path,
                n_ctx=n_ctx,
                n_gpu_layers=n_gpu_layers,
                n_threads=n_threads,
                max_tokens=max_tokens,
            )

            total = len(pdfs)
            for index, pdf_path in enumerate(pdfs, start=1):
                self.events.put(
                    ("status", f"Processing {index}/{total}: {pdf_path.name}")
                )
                record = self._process_pdf(
                    pdf_path=pdf_path,
                    fields=fields,
                    extractor=extractor,
                    dpi=dpi,
                )
                self.events.put(("record", record))
                self.events.put(("progress", index / total * 100))

            self.events.put(("done", f"Completed {total} PDF(s)."))
        except Exception:
            self.events.put(("fatal", traceback.format_exc()))
        finally:
            if extractor is not None:
                extractor.close()

    def _process_pdf(
        self,
        pdf_path: Path,
        fields: list[str],
        extractor: QwenVLExtractor,
        dpi: int,
    ) -> ExtractionRecord:
        try:
            with fitz.open(pdf_path) as document:
                page_count = document.page_count
                if page_count == 0:
                    raise ValueError("PDF contains no pages.")

                indices = [0] if page_count == 1 else [0, page_count - 1]
                display_pages = [idx + 1 for idx in indices]

                texts: list[str] = []
                images: list[tuple[int, str]] = []

                for idx in indices:
                    page = document.load_page(idx)
                    texts.append(page.get_text("text", sort=True) or "")
                    images.append((idx + 1, render_page_to_data_uri(page, dpi)))

                first_text = texts[0]
                last_text = texts[-1]
                qc = text_layer_qc(first_text, last_text)
                source_text = first_text
                if last_text != first_text:
                    source_text += "\n" + last_text

                prompt = build_prompt(fields, display_pages, qc)
                extracted, raw = extractor.extract(prompt, images, fields)
                fq = field_qc_against_text(extracted, source_text, qc.status)

                return ExtractionRecord(
                    filename=pdf_path.name,
                    path=str(pdf_path),
                    page_count=page_count,
                    selected_pages=display_pages,
                    text_layer_qc=qc,
                    extracted_fields=extracted,
                    field_qc=fq,
                    model_raw_response=raw,
                )

        except Exception as exc:
            return ExtractionRecord(
                filename=pdf_path.name,
                path=str(pdf_path),
                page_count=0,
                selected_pages=[],
                text_layer_qc=TextLayerQC(
                    status="ERROR",
                    warnings=["PDF processing failed before QC could complete."],
                ),
                extracted_fields={field: None for field in fields},
                field_qc=[
                    FieldQC(
                        field=field,
                        present=False,
                        found_in_text_layer=None,
                        match_score=None,
                        note="Processing error.",
                    )
                    for field in fields
                ],
                error=f"{type(exc).__name__}: {exc}",
            )

    def _configure_tree(self, fields: list[str]) -> None:
        for item in self.tree.get_children():
            self.tree.delete(item)

        columns = ["filename", "text_qc", "warnings", *fields]
        self.tree.configure(columns=columns)

        headings = {
            "filename": "Filename",
            "text_qc": "Text QC",
            "warnings": "QC warnings",
        }
        for column in columns:
            self.tree.heading(column, text=headings.get(column, column))
            if column == "filename":
                width = 220
            elif column == "warnings":
                width = 310
            elif column == "text_qc":
                width = 110
            else:
                width = 180
            self.tree.column(column, width=width, minwidth=80, stretch=True)

    @staticmethod
    def _compact_value(value: Any, limit: int = 180) -> str:
        if value is None:
            return ""
        if isinstance(value, str):
            text = value
        else:
            text = json.dumps(value, ensure_ascii=False)
        text = " ".join(text.split())
        return text if len(text) <= limit else text[: limit - 1] + "…"

    def _add_record(self, record: ExtractionRecord) -> None:
        self.records.append(record)
        fields = list(record.extracted_fields)
        warnings = "; ".join(record.text_layer_qc.warnings)
        if record.error:
            warnings = f"{warnings}; {record.error}".strip("; ")

        values = [
            record.filename,
            record.text_layer_qc.status,
            warnings,
            *[
                self._compact_value(record.extracted_fields.get(field))
                for field in fields
            ],
        ]
        self.tree.insert("", "end", iid=str(len(self.records) - 1), values=values)

    def _show_record(self, _event: tk.Event) -> None:
        selection = self.tree.selection()
        if not selection:
            return
        record = self.records[int(selection[0])]

        window = tk.Toplevel(self)
        window.title(record.filename)
        window.geometry("900x700")

        text = tk.Text(window, wrap="word")
        scroll = ttk.Scrollbar(window, command=text.yview)
        text.configure(yscrollcommand=scroll.set)
        text.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")

        text.insert("1.0", json.dumps(asdict(record), indent=2, ensure_ascii=False))
        text.configure(state="disabled")

    def _poll_events(self) -> None:
        try:
            while True:
                event, payload = self.events.get_nowait()
                if event == "status":
                    self.status_var.set(payload)
                elif event == "progress":
                    self.progress_var.set(payload)
                elif event == "record":
                    self._add_record(payload)
                elif event == "done":
                    self.status_var.set(payload)
                    self.run_button.configure(state="normal")
                elif event == "fatal":
                    self.status_var.set("Processing failed.")
                    self.run_button.configure(state="normal")
                    messagebox.showerror("Processing failed", payload)
        except Empty:
            pass
        self.after(100, self._poll_events)

    def _clear(self) -> None:
        if self.worker and self.worker.is_alive():
            messagebox.showinfo("Busy", "Wait for the current batch to finish before clearing.")
            return
        self.records.clear()
        for item in self.tree.get_children():
            self.tree.delete(item)
        self.progress_var.set(0)
        self.status_var.set("Ready.")

    def _export_json(self) -> None:
        if not self.records:
            messagebox.showinfo("Nothing to export", "Run an extraction first.")
            return
        path = filedialog.asksaveasfilename(
            title="Export JSON",
            defaultextension=".json",
            filetypes=[("JSON", "*.json")],
        )
        if not path:
            return
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(
                [asdict(record) for record in self.records],
                handle,
                indent=2,
                ensure_ascii=False,
            )
        self.status_var.set(f"Exported JSON to {path}")

    def _export_csv(self) -> None:
        if not self.records:
            messagebox.showinfo("Nothing to export", "Run an extraction first.")
            return
        path = filedialog.asksaveasfilename(
            title="Export CSV",
            defaultextension=".csv",
            filetypes=[("CSV", "*.csv")],
        )
        if not path:
            return

        field_names = list(self.records[0].extracted_fields)
        columns = [
            "filename",
            "path",
            "page_count",
            "selected_pages",
            "text_qc_status",
            "text_qc_warnings",
            *field_names,
            "field_qc_json",
            "error",
        ]

        with open(path, "w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=columns)
            writer.writeheader()
            for record in self.records:
                row: dict[str, Any] = {
                    "filename": record.filename,
                    "path": record.path,
                    "page_count": record.page_count,
                    "selected_pages": json.dumps(record.selected_pages),
                    "text_qc_status": record.text_layer_qc.status,
                    "text_qc_warnings": "; ".join(record.text_layer_qc.warnings),
                    "field_qc_json": json.dumps(
                        [asdict(item) for item in record.field_qc],
                        ensure_ascii=False,
                    ),
                    "error": record.error,
                }
                for name in field_names:
                    value = record.extracted_fields.get(name)
                    row[name] = (
                        value if isinstance(value, str)
                        else json.dumps(value, ensure_ascii=False)
                    )
                writer.writerow(row)

        self.status_var.set(f"Exported CSV to {path}")


def main() -> None:
    app = MetadataExtractorGUI()
    app.mainloop()


if __name__ == "__main__":
    main()
