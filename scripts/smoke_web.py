"""Opt-in integration check against the real running local agent, never part of CI."""
import asyncio
from aiohttp import ClientSession

async def main():
    origin = 'http://127.0.0.1:8765'
    async with ClientSession() as client:
        async with client.ws_connect(origin + '/ws', origin=origin) as ws:
            async def turn(text=None):
                events = []
                if text:
                    await ws.send_json({'type': 'message', 'text': text})
                async with asyncio.timeout(130):
                    while True:
                        event = await ws.receive_json()
                        events.append(event)
                        if event['type'] == 'error':
                            raise AssertionError(event['message'])
                        if event.get('state') == 'ready':
                            return events
            await turn()
            events = await turn('Verifique a caixa de email X usando a ferramenta e resuma.')
            assert any(e.get('type') == 'tool' and e.get('state') == 'started' for e in events), events
            assert any(e.get('type') == 'tool' and e.get('state') == 'done' for e in events), events
            assert any(e.get('state') == 'executing' for e in events), events
            assert any(e.get('role') == 'assistant' and e.get('text') for e in events), events
            print('PASS: consulta MCP real, estados e resposta do Ollama.')
            events = await turn('Qual era o assunto do email da Ana que você acabou de consultar?')
            answer = ' '.join(e.get('text', '') for e in events if e.get('role') == 'assistant')
            assert 'reuni' in answer.lower(), answer
            print('PASS: contexto preservado na segunda mensagem.')

if __name__ == '__main__':
    asyncio.run(main())
