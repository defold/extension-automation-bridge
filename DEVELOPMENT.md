# Development Guardrails

This file records stable, cross-cutting engineering rules. Endpoint behavior,
individual regressions, and feature-specific instructions belong in API docs,
tests, and comments next to the implementation.

## Keep policy in one authoritative layer

Each safety invariant and default should have one owner. The native runtime owns
execution state, completion, and fail-safe cleanup. Callers may choose documented
operation parameters, and wrappers may translate those choices, but they must
not invent or override native safety policy. Raw API clients and wrapper clients
must receive equivalent guarantees.

When responsibility is unclear, decide and document the owner before adding
another timer, lease calculation, retry loop, or cleanup path.

## Design for the embedded platform that actually ships

Do not assume a general-purpose library behaves like its desktop counterpart.
Verify supported behavior in the Defold version used by the extension and keep
platform constraints behind a shared adapter.

One current transport constraint is that Defold's embedded DLIB HTTP server has
reason phrases only for `200`, `302`, `404`, and `500`. All responses must pass
through `SendResponse()`, which maps unsupported success statuses to `200` and
unsupported error statuses to `500`, while the JSON error envelope preserves the
logical status in `error.status`. A compatibility adapter must never turn a
failure into a successful transport response. Do not call
`dmWebServer::SetStatusCode()` directly or remove the mapping until every
supported Defold target is verified and covered by integration tests.

## Evolve protocols explicitly

Never rely on an older endpoint rejecting an unknown field; it may ignore the
field and return a misleading success. Any additive change that alters behavior
must have a capability or protocol version. Clients should negotiate that
version before sending the new behavior and fail clearly when it is unavailable.

Preserve compatibility for operations whose semantics did not change.

## Reject malformed input instead of applying defaults

Treat an absent optional value differently from a supplied value that failed to
parse. Check typed-parser results whenever a field is present. Invalid values
must produce a structured error and must not silently become zero, false, an
empty value, or another default.

Apply the same rule in native endpoints and public wrappers so failures occur as
early and consistently as possible.

## Keep the default test suite fast and deterministic

Do not turn a supported maximum duration into an equivalent wall-clock wait in
the normal suite. Check numeric boundaries with unit tests, exercise lifecycle
behavior with short integration cases, and reserve slow real-time boundaries or
platform experiments for explicit release checks.

Tests should assert observable contracts rather than untouched initial state or
timing accidents that external input and asynchronous rendering can change.

Run the dependency-free suite from the repository root:

```sh
PYTHONPATH=automation_bridge/automation-bridge-python python3 -m unittest tests.test_automation_bridge_api tests.test_tooling
```
