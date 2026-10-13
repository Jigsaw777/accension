# Windows desktop tutorial

For Windows end users. No Python, Node, Docker or administrator setup is needed. [Documentation index](../README.md) · [Next: Workbench](workbench.md).

Accension uses a local browser interface backed by a user-level Python service. Its installer bundles CPython and provider/LAN dependencies. Installation works offline and needs neither a separate Python installation nor administrator rights. Windows 10 1809+ x64 is required.

## Install and open

1. Download the [latest main Windows build](https://github.com/Jigsaw777/accension/releases/download/windows-main/Accension-latest-windows-x64.exe), or use `dist/windows/Accension-latest-windows-x64.exe` after a local build. The download appears after the Windows workflow is merged and completes successfully; source checkouts do not contain generated installers.
2. Compare the download with its [SHA-256 file](https://github.com/Jigsaw777/accension/releases/download/windows-main/Accension-latest-windows-x64.exe.sha256). In PowerShell, run `Get-FileHash .\Accension-latest-windows-x64.exe -Algorithm SHA256` and compare the displayed hash.
3. Run the installer. Open **Accension** from the Start menu, or select the optional desktop shortcut.
4. The browser opens Workbench. Choose **Accension settings** to connect providers, configure models and routing, then **Open Workbench** to return.
5. Follow the [Workbench tutorial](workbench.md). You can create projects without a model; automated execution needs an eligible model and registered checks.

The installer is unsigned. Windows may show an unknown-publisher/SmartScreen warning. Verify the source and checksum before deciding whether to run it. Checksums detect mismatches; they are not code-signing certificates.

Application files install to `%LOCALAPPDATA%\Programs\Accension`. Settings, credentials, logs and SQLite state live in `%APPDATA%\accension`, unless `ROUTER_HOME` overrides it. The installer never bundles a developer's credentials or project data. No automatic startup, firewall or assistant registration is installed.

## Update and stop

Download and run the newest installer to update. The same application ID upgrades the existing installation and stops its hub before replacing files. Your profile and registered repositories are preserved. Closing a browser leaves the hub running; **Stop Accension** in the Start menu stops it. Windows **Installed apps → Accension → Uninstall** stops the hub and removes the runtime and shortcuts, keeping the profile.

Stopping waits for the hub's cleanup marker, including open event-stream connections. If cleanup cannot finish, upgrade/uninstall stops with an error rather than replacing a runtime still in use. Inspect service logs and resolve the known worker before retrying.

If a hub launched from a source checkout is already listening on port 8765, stop it with that checkout's `accs stop` first. A desktop launcher can reuse an already running hub, so opening a shortcut alone does not replace code served by an older process. After installing, reload the browser; check that Workbench has the Accension sidebar and green theme.

An offline executable always contains the source snapshot from its build. It cannot obtain future code without downloading another package. The automation updates branch downloads after successful builds; it does not silently replace an installed application or interrupt work. Package version 1.1.0 can have multiple development builds: use `built_at`, `source_commit` and `ui_assets` in the accompanying manifest to distinguish them. Back up the profile before upgrades that migrate the database; downgrading schema version 9 requires backup restoration.

## Latest development build

[Develop Windows build](https://github.com/Jigsaw777/accension/releases/download/windows-develop/Accension-latest-windows-x64.exe) follows successful `develop` builds. These branch downloads are prerelease snapshots. They are available only after that branch contains the workflow and a successful build. PR builds are available through [Windows workflow artifacts](https://github.com/Jigsaw777/accension/actions/workflows/windows-installer.yml), labeled by commit; they are never published to the main/develop download.

## Troubleshoot installation

If the shortcut shows old controls, stop the old service before relaunching and reload the browser. Inspect the downloaded manifest's source commit; package version alone does not identify a build. If port 8765 is occupied, use the existing installation's `accs status` and `accs stop`, rather than killing unrelated Python processes. For provider/execution errors, run `accs doctor` from the installed CLI or inspect **Logs**. [General troubleshooting](../../TROUBLESHOOTING.md).

## Contributor build

Use [installer build instructions](../../installer/windows/README.md). The builder verifies the official runtime archive, stages a fresh wheel, compares all installed assets with current source, generates the Windows icon from the shared SVG, and writes SHA-256/dependency/source manifests. The real installer smoke test checks both themes, served asset hashes, shortcuts, zero inference, upgrade and uninstall in an isolated profile.
