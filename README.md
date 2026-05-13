# Automation Bridge Extension

Debug-only native extension for inspecting a running Defold game and sending input from local automation clients such as Codex.

## Installation

After Defold has fetched the extension into your project, copy the `automation-bridge-python` directory from the extension into your project. When you update the extension, update the copied Python helper directory at the same time so your automation scripts use the matching API.

## Documentation

- Native extension endpoint reference: [`automation_bridge/`](automation_bridge/README.md)
- Dependency-free Python helpers for editor bootstrap, node queries, input gestures, waits, screenshots, and diagnostics: [`automation_bridge/automation-bridge-python/`](automation_bridge/automation-bridge-python/README.md)
