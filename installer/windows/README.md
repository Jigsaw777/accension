# Windows desktop installer

The installer bundles Python, Accension and the Azure, AWS, Google and LAN dependencies. Installation requires no Python, internet connection or administrator account. Windows 10 version 1809 or newer on x64 is required. Model providers still need their own setup.

Run `Accension-1.0.0-windows-x64.exe`, then open **Accension** from the Start menu. The optional desktop shortcut does the same. The launcher starts the local hub in the background and opens Workbench in the default browser. Use **Accension settings** in Workbench to manage models and routing; **Open Workbench** returns to projects and work items. Both pages use the same local service.

The application installs in `%LOCALAPPDATA%\Programs\Accension`. Settings, credentials, logs and workbench state remain in the normal `%APPDATA%\accension` profile. The installer does not copy a developer checkout's configuration or register itself in assistant clients. Use the existing Integrations UI for client registration. If a hub from a source checkout is already running on port 8765, stop it before opening this installation.

Use **Stop Accension** from the Start menu to stop the background hub. Closing a browser tab leaves the hub running. Uninstall through Windows **Installed apps**; uninstall stops the hub and removes application files and shortcuts, while preserving the user profile and registered repositories. Upgrades stop the installed hub before replacing its runtime. There is no automatic startup or update service.

The executable is unsigned. Windows SmartScreen may show a publisher warning. Verify the adjacent `.exe.sha256` checksum before running a downloaded build. Database upgrades create backups; use a backup to restore state before installing an older release.

## Build from this checkout

Install Python 3.11+ and Inno Setup 6 on a Windows build machine, then run from the repository root:

```powershell
python -m pip install build==1.6.1 "httpx>=0.27,<1"
python scripts\build_windows_installer.py --compiler "C:\Program Files (x86)\Inno Setup 6\ISCC.exe"
```

The builder verifies the pinned official CPython runtime archive, builds the public wheel, resolves binary Windows dependencies and checks bundled imports/assets before compiling. Build requires internet; end-user installation is offline. Output is `dist\windows\Accension-1.0.0-windows-x64.exe`, with its checksum and resolved dependency manifest. Build outputs stay ignored by Git. Commit the source under `installer\windows` and the builder under `scripts`; distribute generated executables as release artifacts.
