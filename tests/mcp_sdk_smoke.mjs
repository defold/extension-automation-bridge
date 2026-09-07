// Optional independent-client integration test. Set MCP_SDK_ROOT to an installed
// @modelcontextprotocol/sdk directory; no npm dependency is added to the server.
import assert from 'node:assert/strict';
import { join, resolve } from 'node:path';
import { pathToFileURL } from 'node:url';
import { writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';

const sdk = process.env.MCP_SDK_ROOT;
const [pluginPath, projectPath, outputPath] = process.argv.slice(2);
if (!sdk || !pluginPath || !projectPath) throw new Error('Set MCP_SDK_ROOT; pass PLUGIN_PATH PROJECT_PATH [SCREENSHOT_PATH]');
const { Client } = await import(pathToFileURL(join(sdk, 'dist/esm/client/index.js')));
const { StdioClientTransport } = await import(pathToFileURL(join(sdk, 'dist/esm/client/stdio.js')));
let transport = new StdioClientTransport({ command: process.env.MCP_PYTHON || 'python3',
    args: [resolve(pluginPath, 'scripts/run_mcp.py')], cwd: tmpdir(), stderr: 'pipe' });
let client = new Client({name: 'automation-bridge-sdk-smoke', version: '1'}, {capabilities: {}});
let session, game, owned = false;
const report = {};
const call = async (name, args = {}, options = {}) => {
    const result = await client.callTool({name, arguments: {...(session ? {mcp_session: session} : {}), ...args}}, undefined,
        {timeout: 120000, ...options});
    assert.equal(result.isError, false, JSON.stringify(result.structuredContent));
    assert.equal(result.structuredContent.ok, true);
    return result;
};
try {
    await client.connect(transport);
    let cursor, tools = [];
    do {
        const page = await client.listTools(cursor ? {cursor} : {});
        tools.push(...page.tools);
        cursor = page.nextCursor;
    } while (cursor);
    assert.equal(new Set(tools.map(tool => tool.name)).size, tools.length);
    for (const name of ['defold_compile', 'defold_bob', 'defold_observe', 'defold_screenshot', 'automation_bridge_session'])
        assert(tools.some(tool => tool.name === name), name);
    report.tools = tools.length;
    session = (await call('automation_bridge_session', {action: 'open'})).structuredContent.data.mcp_session;
    const project = (await call('defold_open_project', {project_path: resolve(projectPath)})).structuredContent.data;
    const capabilities = (await call('defold_editor_capabilities', {project})).structuredContent.data.commands;
    report.commands = capabilities.map(command => command.name);
    if (report.commands.includes('compile')) {
        const compile = (await call('defold_compile', {project})).structuredContent.data;
        assert(compile.completed && compile.success);
        report.compile = true;
        const bob = (await call('defold_bob', {project, options: {help: true}})).structuredContent.data;
        assert(bob.completed && bob.success);
        report.bob = true;
    } else {
        const unsupported = await client.callTool({name: 'defold_compile', arguments: {project, mcp_session: session}});
        assert.equal(unsupported.structuredContent.error.data.minimum_version, '1.13.2');
        report.legacy_compile_rejected = true;
    }
    const build = (await call('defold_build_and_run', {project, required_capabilities: ['elements', 'input.key>=2', 'application.catalog']})).structuredContent.data;
    game = build.engine; owned = build.session.owns_engine;
    assert(owned);
    report.build = {command: build.build_result.command, completed: build.build_result.completed, success: build.build_result.success, target_url: build.build_result.target_url, issue_count: build.build_result.issues.length};
    const page = (await call('defold_find_elements', {engine: game, selector: {limit: 1}})).structuredContent.data;
    assert.equal(page.elements.length, 1);
    assert(page.next_cursor);
    const second = (await call('defold_find_elements', {engine: game, selector: {limit: 1, cursor: page.next_cursor}})).structuredContent.data;
    assert.equal(second.offset, 1);
    const catalog = (await call('defold_application_catalog', {engine: game, kind: 'command', limit: 5})).structuredContent.data;
    assert(catalog.entries.length > 0);
    const observation = await call('defold_observe', {engine: game, selector: {limit: 3}});
    const images = observation.content.filter(item => item.type === 'image');
    assert.equal(images.length, 1);
    assert.equal(images[0].mimeType, 'image/png');
    if (outputPath) await writeFile(outputPath, Buffer.from(images[0].data, 'base64'));
    report.image_bytes = Buffer.from(images[0].data, 'base64').length;
    report.observation_frames = {elements: observation.structuredContent.data.page.engine_frame,
                                screenshot: observation.structuredContent.data.screenshot.frame};
    const preview = await call('defold_preview', {project, path: '/main/main.collection'});
    assert.equal(preview.content.filter(item => item.type === 'image').length, 1);
    report.preview = true;
    const attached = (await call('defold_connect_engine', {project})).structuredContent.data;
    const info = (await call('defold_session_info', {engine: attached})).structuredContent.data;
    assert.equal(info.owns_engine, false);
    await call('defold_close', {engine: attached});
    await call('defold_health', {engine: game});
    report.borrowed_close_keeps_engine = true;
    const otherSession = (await call('automation_bridge_session', {action: 'open'})).structuredContent.data.mcp_session;
    const observer = (await call('defold_connect_engine', {mcp_session: otherSession, port: build.session.port})).structuredContent.data;
    // Real request cancellation through the independent SDK, followed by a
    // native input queue observation after cleanup has completed.
    const controller = (await call('automation_bridge_get', {operation: 'automation_bridge.engine.Client.input', target: game})).structuredContent.data;
    const held = (await call('defold_key', {engine: game, key: 'M', hold: 2, wait: 'started', timeout: 5})).structuredContent.data;
    await call('defold_health', {engine: observer, mcp_session: otherSession});
    const competing = await client.callTool({name: 'defold_key', arguments: {engine: observer, mcp_session: otherSession, key: 'M'}});
    assert.equal(competing.structuredContent.error.data.code, 'input_controller_busy');
    report.concurrent_native_ownership = true;
    const cancellation = new AbortController();
    const input = call('automation_bridge_call', {operation: 'automation_bridge.engine.InputController.wait', target: controller,
                                                 arguments: {input_or_id: held.input_id, state: 'released', timeout: 5}}, {signal: cancellation.signal});
    const cancelTimer = setTimeout(() => cancellation.abort(), 150);
    await assert.rejects(input, error => {
        report.cancel_rejection = String(error);
        assert.match(String(error), /AbortError/, String(error));
        return true;
    });
    clearTimeout(cancelTimer);
    const deadline = Date.now() + 5000;
    while (true) {
        const status = await client.callTool({name: 'automation_bridge_session', arguments: {mcp_session: session, action: 'info'}});
        if (status.structuredContent.data.cleanup_errors.some(entry => entry.error.code === 'operation_cancelled')) break;
        assert(Date.now() < deadline, 'cancellation did not reach the runtime');
        await new Promise(resolve => setTimeout(resolve, 20));
    }
    const pending = (await call('automation_bridge_call', {operation: 'automation_bridge.engine.InputController.pending', target: controller})).structuredContent.data;
    assert.equal(pending.length, 0, JSON.stringify(pending));
    report.input_after_cancel = pending;
    const cancelledReceipt = (await call('automation_bridge_call', {operation: 'automation_bridge.engine.InputController.status', target: controller,
                                                                    arguments: {input_id: held.input_id}})).structuredContent.data;
    assert.equal(cancelledReceipt.state, 'cancelled');
    assert(cancelledReceipt.actual_duration < 1.5, JSON.stringify(cancelledReceipt));
    report.cancelled = true;
    await call('automation_bridge_session', {mcp_session: otherSession, action: 'close'});
    await call('automation_bridge_session', {action: 'close'});
    await client.close();
    // A new process must reconnect with new handles while the borrowed engine lives.
    session = undefined;
    transport = new StdioClientTransport({command: process.env.MCP_PYTHON || 'python3', args: [resolve(pluginPath, 'scripts/run_mcp.py')], cwd: tmpdir(), stderr: 'pipe'});
    client = new Client({name: 'automation-bridge-sdk-reconnect', version: '1'}, {capabilities: {}});
    await client.connect(transport);
    await client.listTools();
    const previousGame = game;
    game = (await call('defold_connect_engine', {port: build.session.port})).structuredContent.data;
    const staleHandle = await client.callTool({name: 'defold_health', arguments: {engine: previousGame}});
    assert.equal(staleHandle.structuredContent.error.code, 'unknown_handle');
    await call('defold_health', {engine: game});
    report.reconnected_after_server_restart = true;
} catch (error) {
    console.error(JSON.stringify(report, null, 2));
    throw error;
} finally {
    if (game && owned) await call('defold_close_engine', {engine: game, confirm: true}).catch(error => { report.close_error = String(error); });
    if (session) await call('automation_bridge_session', {action: 'close'}).catch(error => { report.session_close_error = String(error); });
    await client.close();
}
console.log(JSON.stringify(report, null, 2));
assert.equal(report.close_error, undefined, report.close_error);
assert.equal(report.session_close_error, undefined, report.session_close_error);
