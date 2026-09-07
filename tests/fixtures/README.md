# Editor API fixtures

`editor_openapi_1_13_1.json` records the legacy command enum.
`editor_openapi_1_13_2.json` records individual command paths, focus, and Bob
authentication/request schemas. These are reduced OpenAPI fixtures: unrelated
documentation and most UI-only commands are omitted from the newer fixture.
The deprecated `/command/build` alias is deliberately absent in 1.13.2 discovery.
Both API documents use `info.version = "1.0"`; it is not the Defold version.

Reference contracts are in Defold's `editor/src/clj/editor/command_requests.clj`,
`editor/src/clj/editor/web_server.clj`, and
`editor/test/integration/web_server_test.clj`. The 1.13.1 contract comes from its
release tag; the 1.13.2 contract comes from the development source introducing
compile/run and Bob. Some early 1.13.2 alpha installations may lack these additions.
Update the fixtures when those source contracts change, without deriving expected
paths from the Python wrapper's own supported-command constants.
