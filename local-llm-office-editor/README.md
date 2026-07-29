# Local LLM Office Editor

This VBA macro connects Microsoft Word and classic Outlook to a local
`llama-cpp-python` OpenAI-compatible `POST /v1/chat/completions` endpoint.

- In Word, the selected text is replaced while Track Changes is temporarily
  enabled. The document's previous Track Changes setting is then restored.
- In classic Outlook, the original selected text is retained and the proposed
  revision is appended immediately after it in square brackets:
  `original text [suggested revision]`.
- `LLM_EditSelection` asks for an optional instruction such as “make this more
  concise” or “make the tone warmer”.
- `<think>...</think>`, `<thinking>...</thinking>`, and
  `<analysis>...</analysis>` sections are removed before any text is inserted.
- The system prompt is an ordinary UTF-8 text file under the current user's
  roaming AppData folder.

The endpoint is restricted to `localhost`, `127.0.0.1`, or `::1` by default so
selected text is not accidentally sent off the computer.

## Files

- `LocalLlmOfficeEditor.bas` — the shared macro module.
- `JsonConverter.bas` — VBA-JSON 2.3.1, used to create and parse JSON.
- `settings.example.ini` — configuration reference.
- `system-prompt.example.txt` — default editable system prompt.

## Install in Word

1. Open Word and press `Alt+F11`.
2. In the VBA editor, select the `Normal` project (or a specific `.dotm`
   template).
3. Choose **File > Import File** and import:
   - `LocalLlmOfficeEditor.bas`
   - `JsonConverter.bas`
4. Choose **Tools > References** and enable **Microsoft Scripting Runtime**.
   The official VBA-JSON module uses its `Dictionary` type.
5. Save the template as a macro-enabled template if prompted.

## Install in classic Outlook

1. Open classic Outlook and press `Alt+F11`.
2. Select `VbaProject.OTM`.
3. Import the same two `.bas` files.
4. Under **Tools > References**, enable **Microsoft Scripting Runtime**.
5. Save the VBA project and restart Outlook.

New Outlook does not run these VBA macros. For classic Outlook, the macro works
in a popped-out compose window and in an inline reply when Outlook exposes its
Word editor.

Macro security is controlled by Office Trust Center and may be managed by your
organisation. Do not weaken an organisation-managed policy.

## Configure

Run `LLM_CreateDefaultConfiguration` once from either Word or Outlook. It
creates:

```text
%APPDATA%\LocalLlmOfficeEditor\settings.ini
%APPDATA%\LocalLlmOfficeEditor\system-prompt.txt
```

Start `llama-cpp-python` with a GGUF model. On Windows:

```powershell
python -m llama_cpp.server --model "C:\path\to\model.gguf" --model_alias local-edit-model --n_ctx 8192
```

The server package, if it is not already installed, is provided by
`llama-cpp-python[server]`. The default server port is 8000. Use the chat format
appropriate for the model if its GGUF metadata does not already supply a chat
template.

Edit `settings.ini` for the server and loaded model alias:

```ini
endpoint=http://127.0.0.1:8000/v1/chat/completions
model=local-edit-model
api_key=
temperature=0.1
timeout_seconds=120
max_selection_chars=12000
max_tokens=2048
confirm_before_insert=true
allow_non_local_endpoint=false
```

If the server was launched with a different `--port` or `--model_alias`, use
those values here. With a multi-model server configuration, `model` must match
the selected model's `model_alias`.

Keep `allow_non_local_endpoint=false` for a local-only workflow. If a server
requires a token, place it in `api_key`; it is sent as a Bearer token.

Edit `system-prompt.txt` to change the permanent editing rules. The selected
text and per-run instruction are sent separately from the system prompt.

## Use

1. Highlight text in Word or an editable classic Outlook message.
2. Run one of these macros with `Alt+F8`, or assign it to the Quick Access
   Toolbar:
   - `LLM_EditSelection` — asks for an optional editing instruction.
   - `LLM_ProofreadSelection` — spelling, punctuation, and grammar only.
   - `LLM_MakeConcise` — concise rewrite.
3. Review the returned text in the confirmation dialog and choose **Yes**.

In Word, accept or reject the resulting revision through Word's normal Review
tools. In Outlook, the original remains beside the bracketed suggestion so the
author can manually choose or adapt it.

Set `confirm_before_insert=false` only if you want a model response applied
without the confirmation dialog.

## Test and troubleshooting

Run `LLM_SelfTest` after installation. It verifies that VBA-JSON works and that
reasoning tags are removed; it does not contact the model or edit text.

Common issues:

- **User-defined type not defined / Dictionary** — enable **Microsoft Scripting
  Runtime** in that application's VBA project.
- **Connection error** — start the local model server and verify `endpoint` and
  `model`.
- **No editable Outlook body** — place the cursor in a classic Outlook compose
  window or inline reply and select body text.
- **Word cannot apply the revision** — the document may be protected or
  restricted for editing.
- **Macro not listed** — import into a standard module, not a class module, and
  restart the Office application after saving.

## Privacy behavior

The macro sends only:

- the selected text;
- the instruction entered for that run;
- the editable system prompt; and
- the configured model/request parameters.

It does not send the whole document, email subject, recipients, attachments, or
surrounding body text. Server-side logging and retention are controlled by the
local model server.

## Third-party component

`JsonConverter.bas` is from
[VBA-tools/VBA-JSON](https://github.com/VBA-tools/VBA-JSON), version 2.3.1,
and carries its upstream MIT/BSD notices in the source file.
