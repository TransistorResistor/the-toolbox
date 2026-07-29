from __future__ import annotations

import argparse
import ctypes
import os
import queue
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import tkinter as tk
from tkinter import filedialog, messagebox, ttk


APP_TITLE = "SafeTensors to GGUF Quantizer"

CONVERT_SCRIPT = "convert_hf_to_gguf.py"
CUDA_WHEELS = ["cu124", "cu125", "cu130", "cu132", "cu123", "cu122", "cu121", "cu118", "cpu"]
BASE_OUTTYPES = ["f16", "bf16", "f32", "q8_0", "auto"]
QUANT_TYPES = [
    "Q4_K_M",
    "Q5_K_M",
    "Q6_K",
    "Q8_0",
    "Q4_K_S",
    "Q5_K_S",
    "Q3_K_M",
    "Q3_K_L",
    "Q2_K",
    "IQ4_NL",
    "IQ4_XS",
    "IQ3_M",
    "IQ2_M",
    "F16",
    "BF16",
    "F32",
    "COPY",
]

FTYPE_FALLBACKS = {
    "F32": 0,
    "F16": 1,
    "Q4_0": 2,
    "Q4_1": 3,
    "Q8_0": 7,
    "Q5_0": 8,
    "Q5_1": 9,
    "Q2_K": 10,
    "Q3_K_S": 11,
    "Q3_K_M": 12,
    "Q3_K_L": 13,
    "Q4_K_S": 14,
    "Q4_K_M": 15,
    "Q4_K": 15,
    "Q5_K_S": 16,
    "Q5_K_M": 17,
    "Q5_K": 17,
    "Q6_K": 18,
    "IQ2_XXS": 19,
    "IQ2_XS": 20,
    "Q2_K_S": 21,
    "IQ3_XS": 22,
    "IQ3_XXS": 23,
    "IQ1_S": 24,
    "IQ4_NL": 25,
    "IQ3_S": 26,
    "IQ3_M": 27,
    "IQ2_S": 28,
    "IQ2_M": 29,
    "IQ4_XS": 30,
    "IQ1_M": 31,
    "BF16": 32,
    "TQ1_0": 36,
    "TQ2_0": 37,
}


@dataclass(frozen=True)
class JobConfig:
    python_exe: Path
    llama_cpp_dir: Path
    model_dir: Path
    output_dir: Path
    base_outtype: str
    quant_type: str
    threads: int
    multimodal: bool
    keep_intermediate: bool
    allow_requantize: bool
    leave_output_tensor: bool
    pure: bool
    keep_split: bool
    model_name: str


class LogPump:
    def __init__(self, emit: Callable[[str], None]) -> None:
        self.emit = emit

    def __call__(self, line: str = "") -> None:
        timestamp = time.strftime("%H:%M:%S")
        self.emit(f"[{timestamp}] {line}".rstrip())


def find_converter(llama_cpp_dir: Path) -> Path:
    candidates = [
        llama_cpp_dir / CONVERT_SCRIPT,
        llama_cpp_dir / "convert-hf-to-gguf.py",
        llama_cpp_dir / "examples" / CONVERT_SCRIPT,
    ]
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    raise FileNotFoundError(
        f"Could not find {CONVERT_SCRIPT}. Select a current llama.cpp checkout directory."
    )


def default_output_name(model_dir: Path, suffix: str) -> str:
    safe_name = model_dir.name.replace(" ", "-")
    return f"{safe_name}-{suffix}.gguf"


def stream_subprocess(command: list[str], cwd: Path, log: LogPump) -> None:
    log("Running: " + subprocess.list2cmdline(command))
    process = subprocess.Popen(
        command,
        cwd=str(cwd),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
    )
    assert process.stdout is not None
    for line in process.stdout:
        log(line.rstrip())
    return_code = process.wait()
    if return_code:
        raise RuntimeError(f"Command failed with exit code {return_code}.")


def convert_hf_to_gguf(
    python_exe: Path,
    converter: Path,
    model_dir: Path,
    outfile: Path,
    outtype: str,
    model_name: str,
    mmproj: bool,
    log: LogPump,
) -> None:
    command = [
        str(python_exe),
        str(converter),
        str(model_dir),
        "--outfile",
        str(outfile),
        "--outtype",
        outtype,
    ]
    if model_name.strip():
        command.extend(["--model-name", model_name.strip()])
    if mmproj:
        command.append("--mmproj")
    stream_subprocess(command, converter.parent, log)


def quantize_via_subprocess(
    python_exe: Path,
    input_file: Path,
    output_file: Path,
    quant_type: str,
    threads: int,
    allow_requantize: bool,
    leave_output_tensor: bool,
    pure: bool,
    keep_split: bool,
    log: LogPump,
) -> None:
    command = [
        str(python_exe),
        str(Path(__file__).resolve()),
        "--quantize-cli",
        "--input",
        str(input_file),
        "--output",
        str(output_file),
        "--type",
        quant_type,
        "--threads",
        str(threads),
    ]
    if allow_requantize:
        command.append("--allow-requantize")
    if leave_output_tensor:
        command.append("--leave-output-tensor")
    if pure:
        command.append("--pure")
    if keep_split:
        command.append("--keep-split")
    stream_subprocess(command, Path.cwd(), log)


def resolve_ftype(llama_cpp_module: object, quant_type: str) -> int:
    normalized = quant_type.upper()
    if normalized == "COPY":
        normalized = "F32"
    constant_name = "LLAMA_FTYPE_ALL_F32" if normalized == "F32" else f"LLAMA_FTYPE_MOSTLY_{normalized}"
    if hasattr(llama_cpp_module, constant_name):
        return int(getattr(llama_cpp_module, constant_name))
    if normalized in FTYPE_FALLBACKS:
        return FTYPE_FALLBACKS[normalized]
    raise ValueError(f"Unknown quantization type: {quant_type}")


def run_quantize_cli(args: argparse.Namespace) -> int:
    try:
        try:
            import llama_cpp

            if not hasattr(llama_cpp, "llama_model_quantize"):
                from llama_cpp import llama_cpp as llama_cpp
        except ImportError:
            from llama_cpp import llama_cpp as llama_cpp

        input_path = Path(args.input)
        output_path = Path(args.output)
        if not input_path.is_file():
            raise FileNotFoundError(f"Input GGUF does not exist: {input_path}")
        output_path.parent.mkdir(parents=True, exist_ok=True)

        if hasattr(llama_cpp, "llama_backend_init"):
            llama_cpp.llama_backend_init()
        params = llama_cpp.llama_model_quantize_default_params()
        params.nthread = max(0, int(args.threads))
        params.ftype = resolve_ftype(llama_cpp, args.type)
        params.allow_requantize = bool(args.allow_requantize)
        params.quantize_output_tensor = not bool(args.leave_output_tensor)
        if hasattr(params, "only_copy"):
            params.only_copy = args.type.upper() == "COPY"
        if hasattr(params, "pure"):
            params.pure = bool(args.pure)
        if hasattr(params, "keep_split"):
            params.keep_split = bool(args.keep_split)

        print(f"Quantizing {input_path} -> {output_path} as {args.type}", flush=True)
        result = llama_cpp.llama_model_quantize(
            os.fsencode(input_path),
            os.fsencode(output_path),
            ctypes.byref(params),
        )
        if hasattr(llama_cpp, "llama_backend_free"):
            llama_cpp.llama_backend_free()
        if result != 0:
            raise RuntimeError(f"llama_model_quantize returned {result}")
        print("Quantization complete.", flush=True)
        return 0
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr, flush=True)
        return 1


def run_job(config: JobConfig, emit: Callable[[str], None]) -> None:
    log = LogPump(emit)
    converter = find_converter(config.llama_cpp_dir)
    config.output_dir.mkdir(parents=True, exist_ok=True)

    if not (config.model_dir / "config.json").is_file():
        raise FileNotFoundError("Model directory must contain config.json.")
    if not list(config.model_dir.glob("*.safetensors")):
        log("No .safetensors files found at the top level. The converter may still work for sharded or nested layouts.")

    base_suffix = config.base_outtype.lower()
    base_file = config.output_dir / default_output_name(config.model_dir, base_suffix)
    quant_file = config.output_dir / default_output_name(config.model_dir, config.quant_type.lower())

    log("Converting text/model weights to GGUF.")
    convert_hf_to_gguf(
        config.python_exe,
        converter,
        config.model_dir,
        base_file,
        config.base_outtype,
        config.model_name,
        False,
        log,
    )

    log("Quantizing text/model GGUF.")
    quantize_via_subprocess(
        config.python_exe,
        base_file,
        quant_file,
        config.quant_type,
        config.threads,
        config.allow_requantize,
        config.leave_output_tensor,
        config.pure,
        config.keep_split,
        log,
    )

    mmproj_base: Path | None = None
    mmproj_quant: Path | None = None
    if config.multimodal:
        mmproj_base = config.output_dir / f"mmproj-{default_output_name(config.model_dir, base_suffix)}"
        mmproj_quant = config.output_dir / f"mmproj-{default_output_name(config.model_dir, 'q8_0')}"
        log("Converting multimodal projector with --mmproj.")
        convert_hf_to_gguf(
            config.python_exe,
            converter,
            config.model_dir,
            mmproj_base,
            "f16" if config.base_outtype == "auto" else config.base_outtype,
            config.model_name,
            True,
            log,
        )
        log("Quantizing multimodal projector to Q8_0.")
        quantize_via_subprocess(
            config.python_exe,
            mmproj_base,
            mmproj_quant,
            "Q8_0",
            config.threads,
            True,
            False,
            False,
            config.keep_split,
            log,
        )

    if not config.keep_intermediate:
        for path in [base_file, mmproj_base]:
            if path and path.exists():
                path.unlink()
                log(f"Deleted intermediate: {path}")

    log(f"Done. Model GGUF: {quant_file}")
    if mmproj_quant:
        log(f"Done. MMPROJ GGUF: {mmproj_quant}")


class QuantizerApp(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title(APP_TITLE)
        self.geometry("980x720")
        self.minsize(860, 620)

        self.log_queue: queue.Queue[str] = queue.Queue()
        self.worker: threading.Thread | None = None

        self.python_var = tk.StringVar(value=sys.executable)
        self.llama_dir_var = tk.StringVar(value="")
        self.model_dir_var = tk.StringVar(value="")
        self.output_dir_var = tk.StringVar(value=str(Path.cwd() / "output"))
        self.base_outtype_var = tk.StringVar(value="f16")
        self.quant_type_var = tk.StringVar(value="Q4_K_M")
        self.cuda_var = tk.StringVar(value="cu124")
        self.threads_var = tk.IntVar(value=max(1, (os.cpu_count() or 4) - 1))
        self.model_name_var = tk.StringVar(value="")
        self.multimodal_var = tk.BooleanVar(value=True)
        self.keep_intermediate_var = tk.BooleanVar(value=False)
        self.allow_requantize_var = tk.BooleanVar(value=False)
        self.leave_output_tensor_var = tk.BooleanVar(value=False)
        self.pure_var = tk.BooleanVar(value=False)
        self.keep_split_var = tk.BooleanVar(value=False)

        self._build_ui()
        self.after(100, self._drain_log_queue)

    def _build_ui(self) -> None:
        self.columnconfigure(0, weight=1)
        self.rowconfigure(0, weight=1)

        notebook = ttk.Notebook(self)
        notebook.grid(row=0, column=0, sticky="nsew")

        run_tab = ttk.Frame(notebook, padding=14)
        setup_tab = ttk.Frame(notebook, padding=14)
        notebook.add(run_tab, text="Convert")
        notebook.add(setup_tab, text="Setup")

        self._build_run_tab(run_tab)
        self._build_setup_tab(setup_tab)

    def _build_run_tab(self, parent: ttk.Frame) -> None:
        parent.columnconfigure(1, weight=1)
        parent.rowconfigure(9, weight=1)

        self._path_row(parent, 0, "Python", self.python_var, file=True)
        self._path_row(parent, 1, "llama.cpp folder", self.llama_dir_var)
        self._path_row(parent, 2, "Model folder", self.model_dir_var)
        self._path_row(parent, 3, "Output folder", self.output_dir_var)

        ttk.Label(parent, text="Base GGUF").grid(row=4, column=0, sticky="w", pady=(10, 4))
        ttk.Combobox(parent, textvariable=self.base_outtype_var, values=BASE_OUTTYPES, width=12, state="readonly").grid(
            row=4, column=1, sticky="w", pady=(10, 4)
        )

        ttk.Label(parent, text="Quant type").grid(row=4, column=2, sticky="w", padx=(18, 6), pady=(10, 4))
        ttk.Combobox(parent, textvariable=self.quant_type_var, values=QUANT_TYPES, width=14, state="readonly").grid(
            row=4, column=3, sticky="w", pady=(10, 4)
        )

        ttk.Label(parent, text="Threads").grid(row=5, column=0, sticky="w", pady=4)
        ttk.Spinbox(parent, from_=0, to=256, textvariable=self.threads_var, width=8).grid(row=5, column=1, sticky="w", pady=4)

        ttk.Label(parent, text="Model name").grid(row=5, column=2, sticky="w", padx=(18, 6), pady=4)
        ttk.Entry(parent, textvariable=self.model_name_var).grid(row=5, column=3, sticky="ew", pady=4)
        parent.columnconfigure(3, weight=1)

        options = ttk.Frame(parent)
        options.grid(row=6, column=0, columnspan=4, sticky="ew", pady=(8, 4))
        for i in range(6):
            options.columnconfigure(i, weight=1)
        ttk.Checkbutton(options, text="Vision/mmproj", variable=self.multimodal_var).grid(row=0, column=0, sticky="w")
        ttk.Checkbutton(options, text="Keep intermediate", variable=self.keep_intermediate_var).grid(row=0, column=1, sticky="w")
        ttk.Checkbutton(options, text="Allow requantize", variable=self.allow_requantize_var).grid(row=0, column=2, sticky="w")
        ttk.Checkbutton(options, text="Leave output tensor", variable=self.leave_output_tensor_var).grid(row=0, column=3, sticky="w")
        ttk.Checkbutton(options, text="Pure", variable=self.pure_var).grid(row=0, column=4, sticky="w")
        ttk.Checkbutton(options, text="Keep split", variable=self.keep_split_var).grid(row=0, column=5, sticky="w")

        actions = ttk.Frame(parent)
        actions.grid(row=7, column=0, columnspan=4, sticky="ew", pady=(10, 8))
        self.run_button = ttk.Button(actions, text="Run Conversion + Quantize", command=self._start_job)
        self.run_button.pack(side="left")
        ttk.Button(actions, text="Clear Log", command=self._clear_log).pack(side="left", padx=8)

        self.status_var = tk.StringVar(value="Idle")
        ttk.Label(parent, textvariable=self.status_var).grid(row=8, column=0, columnspan=4, sticky="w")

        self.log_text = tk.Text(parent, wrap="word", height=22)
        self.log_text.grid(row=9, column=0, columnspan=4, sticky="nsew")
        scroll = ttk.Scrollbar(parent, orient="vertical", command=self.log_text.yview)
        scroll.grid(row=9, column=4, sticky="ns")
        self.log_text.configure(yscrollcommand=scroll.set)

    def _build_setup_tab(self, parent: ttk.Frame) -> None:
        parent.columnconfigure(0, weight=1)
        parent.columnconfigure(1, weight=1)

        install_frame = ttk.LabelFrame(parent, text="Install llama-cpp-python", padding=12)
        install_frame.grid(row=0, column=0, columnspan=2, sticky="ew")
        ttk.Label(install_frame, text="CUDA wheel").grid(row=0, column=0, sticky="w")
        ttk.Combobox(install_frame, textvariable=self.cuda_var, values=CUDA_WHEELS, width=12, state="readonly").grid(
            row=0, column=1, sticky="w", padx=8
        )
        ttk.Button(install_frame, text="Copy pip command", command=self._show_llama_pip_command).grid(
            row=0, column=2, padx=8
        )

        deps_frame = ttk.LabelFrame(parent, text="Conversion dependencies", padding=12)
        deps_frame.grid(row=1, column=0, columnspan=2, sticky="ew", pady=12)
        ttk.Button(deps_frame, text="Copy dependency command", command=self._show_deps_command).grid(row=0, column=0, sticky="w")

        help_text = (
            "Use a current llama.cpp checkout for conversion. The converter handles Hugging Face directories "
            "containing config.json, tokenizer files, and safetensors shards. For supported multimodal models, "
            "the Vision/mmproj option runs the same converter with --mmproj and emits a separate mmproj GGUF."
        )
        ttk.Label(parent, text=help_text, wraplength=880, justify="left").grid(row=2, column=0, columnspan=2, sticky="ew")

    def _path_row(self, parent: ttk.Frame, row: int, label: str, variable: tk.StringVar, file: bool = False) -> None:
        ttk.Label(parent, text=label).grid(row=row, column=0, sticky="w", pady=4)
        ttk.Entry(parent, textvariable=variable).grid(row=row, column=1, columnspan=3, sticky="ew", pady=4, padx=(8, 8))
        command = (lambda: self._browse_file(variable)) if file else (lambda: self._browse_dir(variable))
        ttk.Button(parent, text="Browse", command=command).grid(row=row, column=4, sticky="ew", pady=4)

    def _browse_dir(self, variable: tk.StringVar) -> None:
        selected = filedialog.askdirectory()
        if selected:
            variable.set(selected)

    def _browse_file(self, variable: tk.StringVar) -> None:
        selected = filedialog.askopenfilename(filetypes=[("Python executables", "*.exe"), ("All files", "*.*")])
        if selected:
            variable.set(selected)

    def _show_llama_pip_command(self) -> None:
        tag = self.cuda_var.get()
        command = f'{self.python_var.get()} -m pip install --upgrade llama-cpp-python --extra-index-url https://abetlen.github.io/llama-cpp-python/whl/{tag}'
        self.clipboard_clear()
        self.clipboard_append(command)
        self._append_log("Install command:")
        self._append_log(command)

    def _show_deps_command(self) -> None:
        command = (
            f'{self.python_var.get()} -m pip install --upgrade torch transformers safetensors '
            "sentencepiece protobuf numpy pyyaml gguf mistral-common"
        )
        self.clipboard_clear()
        self.clipboard_append(command)
        self._append_log("Conversion dependency command:")
        self._append_log(command)

    def _start_job(self) -> None:
        if self.worker and self.worker.is_alive():
            messagebox.showinfo(APP_TITLE, "A job is already running.")
            return

        try:
            config = JobConfig(
                python_exe=Path(self.python_var.get()).expanduser(),
                llama_cpp_dir=Path(self.llama_dir_var.get()).expanduser(),
                model_dir=Path(self.model_dir_var.get()).expanduser(),
                output_dir=Path(self.output_dir_var.get()).expanduser(),
                base_outtype=self.base_outtype_var.get(),
                quant_type=self.quant_type_var.get(),
                threads=int(self.threads_var.get()),
                multimodal=bool(self.multimodal_var.get()),
                keep_intermediate=bool(self.keep_intermediate_var.get()),
                allow_requantize=bool(self.allow_requantize_var.get()),
                leave_output_tensor=bool(self.leave_output_tensor_var.get()),
                pure=bool(self.pure_var.get()),
                keep_split=bool(self.keep_split_var.get()),
                model_name=self.model_name_var.get(),
            )
            self._validate_config(config)
        except Exception as exc:
            messagebox.showerror(APP_TITLE, str(exc))
            return

        self.status_var.set("Running")
        self.run_button.configure(state="disabled")
        self.worker = threading.Thread(target=self._worker_main, args=(config,), daemon=True)
        self.worker.start()

    def _validate_config(self, config: JobConfig) -> None:
        if not config.python_exe.exists():
            raise FileNotFoundError("Python executable does not exist.")
        if not config.llama_cpp_dir.is_dir():
            raise FileNotFoundError("Select a llama.cpp checkout directory.")
        find_converter(config.llama_cpp_dir)
        if not config.model_dir.is_dir():
            raise FileNotFoundError("Select a Hugging Face model directory.")
        if config.threads < 0:
            raise ValueError("Threads must be 0 or greater.")

    def _worker_main(self, config: JobConfig) -> None:
        try:
            run_job(config, self.log_queue.put)
            self.log_queue.put("__STATUS__:Complete")
        except Exception as exc:
            self.log_queue.put(f"ERROR: {exc}")
            self.log_queue.put("__STATUS__:Failed")

    def _drain_log_queue(self) -> None:
        while True:
            try:
                line = self.log_queue.get_nowait()
            except queue.Empty:
                break
            if line.startswith("__STATUS__:"):
                self.status_var.set(line.split(":", 1)[1])
                self.run_button.configure(state="normal")
            else:
                self._append_log(line)
        self.after(100, self._drain_log_queue)

    def _append_log(self, line: str) -> None:
        self.log_text.insert("end", line + "\n")
        self.log_text.see("end")

    def _clear_log(self) -> None:
        self.log_text.delete("1.0", "end")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=APP_TITLE)
    parser.add_argument("--quantize-cli", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--input", help="Input GGUF for --quantize-cli")
    parser.add_argument("--output", help="Output GGUF for --quantize-cli")
    parser.add_argument("--type", default="Q4_K_M", help="llama.cpp quantization type")
    parser.add_argument("--threads", type=int, default=0)
    parser.add_argument("--allow-requantize", action="store_true")
    parser.add_argument("--leave-output-tensor", action="store_true")
    parser.add_argument("--pure", action="store_true")
    parser.add_argument("--keep-split", action="store_true")
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    if args.quantize_cli:
        missing = [name for name in ["input", "output"] if not getattr(args, name)]
        if missing:
            parser.error("--quantize-cli requires --input and --output")
        return run_quantize_cli(args)
    app = QuantizerApp()
    app.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
