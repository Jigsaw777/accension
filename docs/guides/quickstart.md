# Python application quick start

For Windows, Linux and macOS users. Requires Git, Python 3.11+ and network access to install dependencies. For bundled Python use the [Windows installer](windows.md). [Documentation index](../README.md).

## Install a source checkout

```sh
git clone https://github.com/Jigsaw777/accension.git
cd accension
python -m venv .venv
```

Activate in PowerShell:

```powershell
.\.venv\Scripts\Activate.ps1
```

Or in Linux/macOS:

```sh
source .venv/bin/activate
```

In the activated environment:

```sh
python -m pip install ".[vault]"
accs init
accs start --background
accs workbench open
```

Installation uses the checkout, not an assumed PyPI release. The vault extra supports OS keyrings on macOS/Linux; Windows uses DPAPI. Setup writes your profile and can register repository/check settings. Starting/opening the app calls no model and changes no repository source.

Expected result: Workbench at `http://127.0.0.1:8765/ui/workbench`. `accs ui` opens routing/model settings at `/ui`. A model is optional for manual planning. Accension does not install weights.

## Stop and diagnose

```sh
accs status
accs doctor
accs stop
```

Closing the browser does not stop the hub. If port 8765 serves an older checkout, stop that process through its own installation first.

If PowerShell blocks activation, call the environment directly:

```powershell
.\.venv\Scripts\python.exe -m pip install ".[vault]"
.\.venv\Scripts\accs.exe init
.\.venv\Scripts\accs.exe start --background
.\.venv\Scripts\accs.exe workbench open
```

Unix equivalents are `.venv/bin/python` and `.venv/bin/accs`. If `accs` is missing, confirm installation and activation use the same environment. Start diagnostics before changing settings. [Troubleshooting](../../TROUBLESHOOTING.md).

## Next steps

Try the [no-model project](../../README.md#first-project-no-model-needed), then [Workbench](workbench.md). Execution needs providers, eligible models and trusted checks; follow [models and routing](models-and-routing.md).

Route previews do not execute code. Planning/calibration can call models. Approved execution can spend money, change allowed files and run checks. Review quotes, privacy and scope first. [CLI reference](../CLI.md).
