# CuaCode

CLI agent for computer use and more.
> [!WARNING]
> This is still a work in progress, use at your own risk (not a virus, just not my problem if you mess up your computer lol).

## Requirements
Have [Go](<https://go.dev/>) installed
Have [Python](<https://www.python.org/>) installed

## Install
Run the following commands
```bash
# Create a virtual enviroment:
python3 -m venv venv

# If you're on linux/mac
source ./venv/bin/activate
# If you're on windows run a script similar to this, ask your ai to help you if somehow it errors or something lol
\venv\Scripts\Activate

# Install all the requirements (make sure your enviroment is acutally activated)
pip install -r requirements.txt

# Now, cd into tui
cd tui
# and run **THIS WILL REQUIRE GO**
go build .
```

To execute the TUI, you'll need to run ./tui (or something along those lines).

## Providers

Ollama is the only provider that is currently set up.
> API keys are also supported with Ollama, you can change your api key in the `main.py:28` file, change the `API_KEY=None` to `API_KEY="Your_api_key"`
## Platform requirements

- macOS: `pyobjc-framework-Quartz`
- Linux: `xdotool`
- Windows: `pywin32`, `psutil`    

## Adding tools

Create a folder under `tools/` with `Description.md`, `InputSchema.json`, `main.py` (export `run(args, ctx)`). Auto-loaded.

