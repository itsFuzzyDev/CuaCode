# CuaCode

CLI agent for computer use and more.
> **Warning:** large work in progress. Multiple things are in planning.


```bash
python3 -m venv venv
# activate enviroment
pip install -r requirements.txt
python main.py
```

## Providers

Ollama (default), Anthropic, OpenAI, Gemini. Set in `main.py`:

```python
settings = {"provider": "ollama", "model": "..."}
```

> Currently no setup for API keys or actually using differnet providers. WIP.

## Platform requirements

- macOS: `pyobjc-framework-Quartz`
- Linux: `xdotool`
- Windows: `pywin32`, `psutil`

## Adding tools

Create a folder under `tools/` with `Description.md`, `InputSchema.json`, `main.py` (export `run(args, ctx)`). Auto-loaded.

