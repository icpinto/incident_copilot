"""Actual MCP SDK handshake against a controlled Streamable HTTP transport."""
import json

import httpx
import pytest

from incident_copilot.analysis import code_graph as graph
from incident_copilot.core.settings import Settings


@pytest.mark.asyncio
@pytest.mark.parametrize('wire', ['json', 'sse'])
async def test_mcp_http_session_authentication_and_json_hint_normalization(monkeypatch, wire):
    pytest.importorskip('mcp.client.streamable_http')
    requests = []

    def handle(request):
        assert request.headers['authorization'] == 'Bearer private-graph-token'
        if request.method == 'DELETE':
            return httpx.Response(204)
        if request.method == 'GET':
            return httpx.Response(405)
        message = json.loads(request.content)
        requests.append(message)
        if message['method'] == 'notifications/initialized':
            assert request.headers['mcp-session-id'] == 'graph-session'
            return httpx.Response(202)
        if message['method'] == 'initialize':
            payload = {'protocolVersion': message['params']['protocolVersion'],
                       'capabilities': {'tools': {}}, 'serverInfo': {'name': 'gitnexus', 'version': graph.GITNEXUS_VERSION}}
        elif message['method'] == 'tools/list':
            payload = {'tools': [{'name': 'list_repos', 'inputSchema': {'type': 'object'}}]}
        else:
            assert message['method'] == 'tools/call'
            assert message['params']['name'] == 'list_repos'
            payload = {'content': [{'type': 'text', 'text': '{"repositories":[]}\n\n---\n**Next:** provider hint'}]}
        envelope = {'jsonrpc': '2.0', 'id': message['id'], 'result': payload}
        headers = {'mcp-session-id': 'graph-session'}
        if wire == 'sse':
            headers['content-type'] = 'text/event-stream'
            return httpx.Response(200, headers=headers, text='event: message\ndata: ' + json.dumps(envelope) + '\n\n')
        return httpx.Response(200, headers=headers, json=envelope)

    real_client = httpx.AsyncClient
    def factory(**kwargs):
        return real_client(transport=httpx.MockTransport(handle), **kwargs)
    monkeypatch.setattr(graph.httpx, 'AsyncClient', factory)
    config = Settings(_env_file=None, gitnexus_url='http://graph.test/mcp', gitnexus_auth_token='private-graph-token')
    async with graph.open_provider(config) as provider:
        assert provider.version == graph.GITNEXUS_VERSION
        assert await provider.call('list_repos', {'limit': 100, 'offset': 0}) == {'repositories': []}
        with pytest.raises(graph.GraphEvidenceError, match='unsupported_tool'):
            await provider.call('rename', {})
    assert [r['method'] for r in requests] == ['initialize', 'notifications/initialized', 'tools/call', 'tools/list']