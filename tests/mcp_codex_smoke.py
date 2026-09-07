"""Optional installed-plugin discovery check using Codex's own MCP client.

Run with the canonical wrapper on PYTHONPATH. This starts a temporary app-server
without creating a task or invoking a model; it does not exercise UI rendering.
"""
import argparse
import json
from pathlib import Path

from automation_bridge.mcp_runtime import BridgeRuntime
from tests.mcp_client import StdioClient


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--codex', default='codex', help='Codex executable')
    parser.add_argument('--server', default='automation-bridge', help='Installed MCP server name')
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    runtime = BridgeRuntime(root)
    expected = {tool['name'] for tool in runtime.tool_descriptors()}
    runtime.cleanup()
    with StdioClient([args.codex, 'app-server', '--stdio'], cwd=root) as client:
        initialized = client.request('initialize', {
            'clientInfo': {'name': 'automation-bridge-host-check', 'version': '1'},
            'capabilities': {'experimentalApi': True},
        })
        client.send('initialized', notify=True)
        servers, cursor = [], None
        while True:
            page = client.request('mcpServerStatus/list', {
                'detail': 'toolsAndAuthOnly', 'limit': 100,
                **({'cursor': cursor} if cursor else {}),
            }, timeout=60)
            servers.extend(page['data'])
            cursor = page.get('nextCursor')
            if not cursor:
                break
        matches = [server for server in servers if server['name'] == args.server]
        assert len(matches) == 1, 'Install the plugin before running this check: ' + args.server
        observed = set(matches[0]['tools'])
        assert expected <= observed, 'Tools missing in Codex: ' + ', '.join(sorted(expected - observed))
        print(json.dumps({
            'host': initialized.get('userAgent'), 'server': args.server,
            'tool_count': len(observed), 'tools': sorted(observed),
        }, indent=2))


if __name__ == '__main__':
    main()
