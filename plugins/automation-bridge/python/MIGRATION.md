# Python migration guide

This document is for agents updating existing Automation Bridge Python scripts.
It covers only the documented Python API.

Migration sections are ordered newest first. To update across several versions,
apply every applicable section from the oldest required migration toward the
newest.

## Adding a future migration

Add each new migration directly above the previous migration using this form:

```text
## Wrapper <old> → <new> (extension <old> → <new>)

| Wrapper <old> | Wrapper <new> |
| --- | --- |
| Old usage | Replacement |
```

Keep the table limited to behavior that requires script or workflow changes.
Put short examples below it only when a table entry is not sufficient. Do not
rename unrelated variables or valid Defold GUI terms such as `gui_node`,
`gui_node_box`, and `gui_node_text`.

## Wrapper 2.x → 3.0 (extension 2.0.x → 2.1.0)

Most documented high-level calls are unchanged, including `element()`,
`elements()`, `maybe_element()`, `element_by_id()`, `parent()`, `count()`,
`click()`, `drag()`, `type_text()`, and `key()`.

| Wrapper 2.x | Wrapper 3.0 |
| --- | --- |
| Manually update the extension dependency and copied wrapper. | After seeding the complete 3.0 wrapper once, run `project.update_automation_bridge()` as a standalone maintenance step, then restart Python. Pass a version only when the project must be pinned. |
| Capability names `nodes` and `node`. | Use `elements` and `element` in `required_capabilities`, `require()`, `supports()`, health checks, and trace metadata. |
| Direct implementation import `automation_bridge.nodes`. | Use `automation_bridge.elements`; prefer the stable public names `engine.Element` and `engine.Bounds`. |
| Element IDs begin with `n:`. | Element IDs begin with `e:`. Prefer semantic selectors and do not persist IDs across engine runs. |
| A cached element could resolve to a replacement instance during input. | `click(Element)` and `drag(Element, Element)` validate logical identity and may raise `engine.StaleElementError`; re-query the selector before retrying. |
| `key()` accepted arbitrary strings, which could produce successful no-op input. | `key()` accepts one validated key; invalid names raise `ValueError`. Use `type_text()` for literal UTF-8, including braces. |

### Agent procedure

1. Seed the project with the complete wrapper 3.0 directory. Never combine
   modules from wrapper 2.x and 3.0.
2. Run the updater, then exit and restart Python:

   ```python
   from automation_bridge import editor

   project = editor.open_project(".")
   project.update_automation_bridge()
   ```

3. Audit the project's Python files and review every match instead of replacing
   terms globally:

   ```sh
   rg -n 'automation_bridge\.nodes|nodes|node|n:|\.key\(' --glob '*.py'
   ```

4. Apply only the table rows that match the script.
5. Run the affected automation against a debug build and verify selectors before
   sending input.

### Stale-element retry

Prefer selecting the element after a state or scene change. If recovery is
required, re-query it; never retry the same stale object:

```python
from automation_bridge import engine

try:
    game.click(cached_button)
except engine.StaleElementError:
    cached_button = game.element(automation_id="play")
    game.click(cached_button)
```

Passing an ID string cannot provide the logical-identity guard, so pass the
selected `Element` when available.

### Key input

Existing calls such as `game.key("KEY_ENTER")` remain valid. The `KEY_` prefix,
case, and one surrounding brace pair are optional:

```python
game.key("M")
game.key("space")
game.key("{KEY_ESCAPE}")
game.type_text("Hello {player}")
```

Do not pass multiple keys in one `key()` string. Send intentional sequences with
separate calls.

### Verification

- The complete wrapper reports version `3.0.0` through
  `game.trace_metadata()["python_package_version"]`.
- Capability declarations use `elements` and `element`.
- No script logic requires the old `n:` prefix.
- Elements are re-queried after state or scene changes.
- Each `key()` call contains one supported key; literal content uses
  `type_text()`.
