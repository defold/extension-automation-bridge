# OpenAI Tool Integration: GPT-5.6 Programmatic Tool Calling and Other Models

## Recommendation

Use JavaScript for GPT-5.6's internal programmatic tool orchestration, while
keeping Python as Automation Bridge's primary client and scripting language.

For models that do not support Programmatic Tool Calling, expose the same
Automation Bridge capabilities through direct function or MCP tool calls and
run the ordinary tool-calling loop in the API host. Do not rewrite the bridge
client to accommodate a model-specific orchestration feature.

These are separate layers with different responsibilities:

```text
GPT-5.6 with Programmatic Tool Calling
  -> model-generated JavaScript in OpenAI's hosted runtime
  -> eligible function or MCP tools
  -> Python or TypeScript Automation Bridge service/client
  -> Automation Bridge HTTP/JSON API
  -> native Defold extension

Models without Programmatic Tool Calling
  -> direct function or MCP tool calls
  -> Python or TypeScript API host/service
  -> Automation Bridge HTTP/JSON API
  -> native Defold extension
```

The bridge protocol should remain language-neutral. GPT-5.6 does not care
whether a tool is implemented in Python, JavaScript, C++, Rust, or another
language; it interacts with the model-facing tool description, input schema,
any declared output schema, and call result.

## GPT-5.6 programmatic orchestration uses JavaScript

GPT-5.6 supports Programmatic Tool Calling for bounded workflows in which code
can coordinate several tools and reduce their intermediate results. The model
generates JavaScript that runs in an isolated V8 environment.

Generated programs can:

- invoke eligible tools through `tools.*`;
- run independent calls concurrently;
- use loops and conditions;
- perform predictable dependent calls;
- filter, join, rank, deduplicate, aggregate, or validate results;
- return a smaller structured result to the model.

The hosted runtime supports JavaScript with top-level `await`, but it is not a
Node.js environment. It does not provide npm packages, package installation,
direct network access, a general-purpose filesystem, subprocesses, a console,
or persistent JavaScript state. It interacts with external systems only
through tools enabled in the request.

Therefore, when the question is which language GPT-5.6 itself should generate
to coordinate tools, JavaScript is the correct and currently required choice.

See the official [Programmatic Tool Calling
guide](https://developers.openai.com/api/docs/guides/tools-programmatic-tool-calling).

## Model compatibility and fallback

Programmatic Tool Calling is currently documented for the GPT-5.6 family. The
`gpt-5.6` alias routes to `gpt-5.6-sol`; `gpt-5.6-terra` and `gpt-5.6-luna`
provide lower-cost family variants. Check the selected model's page before
enabling Programmatic Tool Calling because tool support is model-specific. See
the official [GPT-5.6 guidance](https://developers.openai.com/api/docs/guides/latest-model).

| Model | Recommended Automation Bridge integration |
| --- | --- |
| GPT-5.6 family | Responses API with direct tools and, for suitable stages, Programmatic Tool Calling |
| [GPT-5.5](https://developers.openai.com/api/docs/models/gpt-5.5) and [GPT-5.4](https://developers.openai.com/api/docs/models/gpt-5.4) | Direct function or MCP tools through the Responses API |
| Older or specialized models | Use only the tools listed as supported on that model's page |

For a model without Programmatic Tool Calling:

- omit the `programmatic_tool_calling` hosted tool;
- omit `allowed_callers`, or use direct-only behavior where supported;
- keep the same Automation Bridge schemas and implementations;
- execute returned tool calls in the API host and return their results to the
  model until it produces a final message.

This fallback changes the orchestration route, not the bridge protocol or the
language used to implement the tool.

## The API host can use Python or JavaScript

The application that calls the OpenAI Responses API can be implemented in
Python or JavaScript. The official Programmatic Tool Calling guide provides
examples for both languages.

Choose the host language based on the surrounding system rather than the model:

| Situation | Recommended language |
| --- | --- |
| Local automation and integration tests | Python |
| Existing Automation Bridge scripts | Python |
| CLI utilities and data processing | Python |
| Node.js backend or web platform | TypeScript |
| ChatGPT Apps SDK integration | TypeScript |
| GPT-5.6 generated orchestration | JavaScript |
| Native Defold integration | C++ |
| Cross-language interoperability | HTTP/JSON or MCP |

## Recommended Automation Bridge architecture

Keep the current division of responsibilities:

- native Defold extension in C++;
- stable, language-neutral HTTP/JSON protocol;
- Python as the primary automation SDK;
- optional TypeScript SDK when Node.js or web users need one;
- MCP or OpenAI function-tool adapter above the client API;
- GPT-5.6 JavaScript orchestration above those tools.

There is no reason to rewrite the Python wrapper in JavaScript solely for
GPT-5.6. A programmatic tool call can invoke a tool backed by Python just as
easily as one backed by JavaScript.

There are two useful exposure patterns:

1. **Function-tool adapter:** the Python or TypeScript OpenAI API host exposes
   function tools and dispatches them to the Python Automation Bridge client.
2. **Remote MCP server:** an MCP server exposes Automation Bridge operations;
   the MCP implementation can use the existing Python client internally.

GPT-5.6 can invoke an eligible MCP tool directly from generated JavaScript. It
does not need an additional function-tool adapter in front of that MCP tool.

For example:

```text
GPT-5.6 generated JavaScript
  |- tools.bridge_scene(...)
  |- tools.bridge_nodes(...)
  |- tools.bridge_drag(...)
  |- tools.bridge_wait_for_event(...)
  `- tools.bridge_screenshot(...)
              |
              v
Python Automation Bridge service/client
              |
              v
Native Defold Automation Bridge
```

## Responses API configuration

Programmatic Tool Calling is a Responses API feature. Add the hosted
`programmatic_tool_calling` tool and opt each eligible tool in with
`allowed_callers`.

For example, a safe read-only function tool can be available through either
route:

```json
[
  {
    "type": "function",
    "name": "bridge_count_nodes",
    "description": "Return the number of Defold scene nodes matching a type filter.",
    "parameters": {
      "type": "object",
      "properties": {
        "type": {
          "type": ["string", "null"],
          "description": "Optional native node type filter."
        }
      },
      "required": ["type"],
      "additionalProperties": false
    },
    "strict": true,
    "output_schema": {
      "type": "object",
      "properties": {
        "count": { "type": "integer" }
      },
      "required": ["count"],
      "additionalProperties": false
    },
    "allowed_callers": ["direct", "programmatic"]
  },
  {
    "type": "programmatic_tool_calling"
  }
]
```

`allowed_callers` controls the invocation route:

| Value | Behavior |
| --- | --- |
| Omitted or `["direct"]` | The model can call the tool directly |
| `["programmatic"]` | Only generated JavaScript can call the tool |
| `["direct", "programmatic"]` | Either route is allowed |

For function tools, `parameters` defines the arguments and `output_schema`
defines the predictable JSON object encoded in the
`function_call_output.output` string. The application must serialize a result
that matches this schema.

Programmatic callers are currently supported for function and custom tools,
MCP, apply patch, local and hosted shell, and code interpreter. Tool search
runs only as a top-level Responses API tool. A deferred function, custom, or
MCP tool must be loaded before a later generated program can invoke it.

## Handling program and tool-call responses

OpenAI executes the generated JavaScript. The API host does not execute the
contents of a `program` item, but it still owns any client-side function calls
that the program issues.

A response can contain these related top-level output items:

- `program`, containing generated JavaScript, a `call_id`, and replay state;
- `function_call`, containing the tool name and arguments, with
  `caller.caller_id` pointing to the program's `call_id`;
- `program_output`, containing the program's final result and status;
- `message`, containing the final model response.

The host must continue the Responses API loop until it receives a final
`message`:

1. Send the request with the hosted Programmatic Tool Calling tool and all
   eligible tools.
2. Preserve every item returned in `response.output`.
3. Execute each returned client-owned `function_call`.
4. Send a `function_call_output` with the original tool `call_id`, a serialized
   output, and, when present, an unchanged copy of `caller`.
5. Handle incomplete responses and repeat the loop as needed.
6. Stop only when a final `message` is present.

The `program_output` and final assistant `message` are separate outputs. Test
both: a program can produce correct structured data while the final message
still omits required evidence or fields.

For stored responses, continue with `previous_response_id`. With `store: false`,
replay the complete ordered sequence, including program, reasoning,
function-call, function-call-output, and program-output items. Stateless
reasoning requests must set `include: ["reasoning.encrypted_content"]` and replay
the returned reasoning items.

## Direct versus programmatic tool calls

Programmatic Tool Calling is useful when the workflow stage is bounded and
predictable, and when code can reduce several intermediate results into a
smaller structured output.

Good candidates include:

- querying and filtering many scene nodes;
- collecting several independent runtime results;
- comparing scene snapshots;
- aggregating profiling data;
- validating structured application state;
- executing a known sequence of dependent read operations.

Prefer direct tool calls when:

- one call is sufficient;
- each result requires fresh model judgment;
- the next step depends on ambiguous visual or semantic interpretation;
- an action requires approval, unless the application deliberately supports
  pausing and resuming the program around that approval;
- an operation has significant side effects and has not been explicitly
  designed for safe programmatic use;
- final citations or native artifacts must be preserved and validated.

A useful Automation Bridge split is:

```text
Programmatic JavaScript
  - inspect many nodes
  - filter candidates
  - aggregate structured state
  - run safe independent reads concurrently
  - validate predictable response shapes

Direct model tool calls
  - choose between semantically ambiguous targets
  - interpret screenshots
  - request or verify approval
  - perform side-effecting actions by default
  - recover from unexpected application state
```

## Tool design matters more than implementation language

Reliable schemas and lifecycle semantics have more impact than choosing Python
or JavaScript for the tool implementation.

Prioritize:

- strict input schemas;
- predictable structured outputs;
- explicit output schemas;
- stable error codes;
- documented error behavior;
- idempotent operations where possible;
- input and action ids;
- native completion receipts;
- explicit timeout and cancellation behavior;
- separate read-only and mutating operations;
- explicit approval boundaries;
- compact results instead of prose;
- documented retry and stopping behavior.

For function inputs, set `strict: true`, set `additionalProperties: false` on
every object, and list every property as required. Represent optional values
with nullable types. Strict mode constrains the arguments generated by the
model; the application must still validate authorization and domain rules.

Programmatic Tool Calling is most reliable when the model knows the return
fields, types, and error behavior before writing the program. If the return
shape is unknown or each result needs semantic interpretation, keep the tool
call direct.

For ordinary direct function calling, a tool result can be a string or a
supported file/image output and does not always need an `output_schema`. For a
predictable function used by Programmatic Tool Calling, prefer a compact JSON
result that matches its declared `output_schema`.

Validate arguments and permissions for every call regardless of whether it is
direct or programmatic. Require application-level approval for high-impact
actions. An MCP tool's `require_approval` policy can pause a generated program,
but writes should remain direct by default unless the pause, resume,
idempotency, and authorization paths have been explicitly tested.

The official GPT-5.6 guidance also recommends exposing only task-relevant tools
and keeping tool descriptions concise and precise. See
[GPT-5.6 model guidance](https://developers.openai.com/api/docs/guides/latest-model).

## Suggested implementation sequence

1. Keep the existing Python client as the reference SDK.
2. Stabilize the language-neutral HTTP/JSON contracts.
3. Add strict structured outputs and error schemas.
4. Expose a focused MCP or OpenAI function-tool surface.
5. Implement and test the ordinary direct tool-calling loop as the portable
   baseline.
6. For GPT-5.6, add the `programmatic_tool_calling` hosted tool and set
   `allowed_callers` on safe, structured tools.
7. Preserve `call_id`, `caller`, program, reasoning, and program-output state
   across every continuation.
8. Keep writes and approval-sensitive tools direct by default.
9. Add a TypeScript SDK only when there is concrete Node.js or web demand.
10. Evaluate direct and programmatic calling on representative workflows.

Evaluation should measure final correctness and evidence coverage alongside
latency, token usage, tool calls, retries, and cost. Fewer calls or tokens are
improvements only when the final result still meets the required quality bar.

## Role of the OpenAI Docs skill

The official
[OpenAI Docs skill](https://github.com/openai/skills/tree/main/skills/.curated/openai-docs)
defines how an agent should obtain current, authoritative OpenAI documentation.
It is a documentation workflow, not a tool runtime or SDK, and does not require
Automation Bridge to use either Python or JavaScript.

## Final decision

- Keep Python for Automation Bridge's primary client and automation scripts.
- Keep the native extension and transport language-neutral.
- Add TypeScript only for ecosystem coverage, not because GPT-5.6 requires it.
- Expose structured function or MCP tools with strict schemas.
- Use GPT-5.6's generated JavaScript for bounded multi-tool orchestration and
  use direct tool calling as the fallback for models without Programmatic Tool
  Calling.
- Configure Programmatic Tool Calling explicitly with
  `programmatic_tool_calling`, `allowed_callers`, and predictable
  `output_schema` contracts.
- Keep the response lifecycle in the API host: execute client-owned calls,
  preserve `call_id` and `caller`, and continue until a final message.
- Use direct model tool calls by default for semantic decisions, approvals,
  visual interpretation, and side-effecting operations.
