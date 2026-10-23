import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from aiohttp import web, ClientSession

from team.app import TeamApp
from team.ids import new_id
from team.vault import MemoryVault
from team.web import TEAM
from web_server import ALLOWED_HOSTS, create_app

CONFIG = {"model": "qwen2.5:7b", "ollama_url": "http://127.0.0.1:11435/v1", "max_concurrent_generations": 1}
PROFILE = dict(goals=["Atender"], personality={"tone": "claro", "initiative": "proativo"}, scope="Escopo",
               out_of_scope="Outros", model_policy_ref="local-primeiro")


class CasesWebCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.folder = tempfile.TemporaryDirectory()
        team_app = TeamApp(Path(self.folder.name), CONFIG, vault=MemoryVault(), legacy_elevenlabs=None, voice_profiles=lambda: [])
        self.app = create_app(object, CONFIG, object(), data_root=Path(self.folder.name), team_app=team_app)
        self.runner = web.AppRunner(self.app)
        await self.runner.setup()
        await web.TCPSite(self.runner, '127.0.0.1', 0).start()
        self.host = f'127.0.0.1:{self.runner.addresses[0][1]}'
        self.base = f'http://{self.host}'
        ALLOWED_HOSTS.add(self.host)
        self.client = ClientSession()
        async with self.client.post(self.base + '/api/persistence/enable', json={}, headers={'Origin': self.base}):
            pass
        self.s = self.app[TEAM].services()
        self.a = self.s.team.create_company(self.s.operator, 'Empresa A')
        self.b = self.s.team.create_company(self.s.operator, 'Empresa B')
        self.ca = self.s.access.tenant_context(self.s.operator, self.a['id'])
        self.cb = self.s.access.tenant_context(self.s.operator, self.b['id'])
        self.dep = self.s.team.create(self.ca, 'department', name='Atendimento', scope='x', status='ativo')
        self.agent = self.s.agents.create_agent(self.ca, department_id=self.dep['id'], name='Suporte', **PROFILE)
        self.other = self.s.agents.create_agent(self.ca, department_id=self.dep['id'], name='Financeiro', **PROFILE)

    async def asyncTearDown(self):
        await self.client.close()
        await self.runner.cleanup()
        ALLOWED_HOSTS.discard(self.host)
        self.folder.cleanup()

    async def call(self, method, path, body=None):
        kwargs = {'headers': {'Origin': self.base}}
        if body is not None:
            kwargs['json'] = body
        async with self.client.request(method, self.base + path, **kwargs) as response:
            text = await response.text()
            return response.status, (json.loads(text) if text.startswith(('{', '[')) else text)

    def t(self, tail, company=None):
        return f'/api/team/companies/{(company or self.a)["id"]}{tail}'

    def case(self, state='em_execucao', ctx=None, dep=None, agent=None):
        ctx = ctx or self.ca
        c = self.s.cases.open_case(ctx, department_id=(dep or self.dep)['id'], objective=f'Demanda {state}', customer_ref='c1')
        if state == 'recebida':
            return c
        c = self.s.cases.transition(ctx, c['id'], c['revision'], 'triagem')
        if state == 'triagem':
            return c
        c = self.s.cases.assign_owner(ctx, c['id'], c['revision'], (agent or self.agent)['id'])
        return self.s.cases.transition(ctx, c['id'], c['revision'], 'em_execucao') if state == 'em_execucao' else c

    def request_approval(self, case, **kwargs):
        options = dict(action='enviar_email', resource='con_x', recipients=['cliente@exemplo.com'], amount_cents=15000,
                       currency='BRL', params={'assunto': 'Cobranca'})
        approval, _ = self.s.approvals.request(self.ca, case['id'], **{**options, **kwargs})
        return approval

    async def snapshot(self, case):
        status, data = await self.call('GET', self.t(f'/cases/{case["id"]}'))
        self.assertEqual(status, 200, data)
        return data


class ListAndSnapshotTests(CasesWebCase):
    async def test_the_list_tells_apart_progress_human_waiting_failure_and_completion(self):
        working = self.case()
        waiting = self.case()
        self.request_approval(waiting)
        failed = self.case()
        self.s.cases.transition(self.ca, failed['id'], failed['revision'], 'falhou', reason='provedor fora')
        done = self.case()
        self.s.cases.add_evidence(self.ca, done['id'], source='recibo', summary='Pago', evidence_type='recibo',
                                  reliability='verificada', content_hash='a' * 64)
        self.s.cases.transition(self.ca, done['id'], done['revision'], 'resolvida')
        asking = self.case()
        self.s.cases.transition(self.ca, asking['id'], asking['revision'], 'aguardando_cliente')
        queued = self.case('triagem')
        self.s.repo  # the human queue is a triage decision sending the case to a person
        with self.s.repo.transaction(self.a['id']) as session:
            session.save({'kind': 'triage_decision', 'schema_version': 1, 'id': new_id('triage_decision'),
                          'tenant_id': self.a['id'], 'revision': 1, 'created_at': '2026-01-01T00:00:00Z',
                          'updated_at': '2026-01-01T00:00:00Z', 'case_id': queued['id'], 'decision': 'fila_humana',
                          'method': 'regra', 'capabilities': [], 'confidence_pct': 0, 'reasons': [{'tipo': 'sem_regra'}],
                          'question': None, 'message_hash': hashlib.sha256(b'x').hexdigest(),
                          'catalog_hash': hashlib.sha256(b'y').hexdigest(), 'threshold_route': 70, 'threshold_clarify': 40,
                          'rounds': 1, 'decided_by': self.s.operator.principal_id})
        status, data = await self.call('GET', self.t('/cases'))
        phases = {c['id']: c['phase'] for c in data['cases']}
        self.assertEqual(phases, {working['id']: 'andamento', waiting['id']: 'aguardando_humano', failed['id']: 'falha',
                                  done['id']: 'concluida', asking['id']: 'aguardando_cliente', queued['id']: 'aguardando_humano'})
        self.assertEqual({c['phase'] for c in data['cases'][:2]}, {'aguardando_humano'})  # people-needed first
        row = next(c for c in data['cases'] if c['id'] == waiting['id'])
        self.assertEqual((row['pending_approvals'], row['owner_name'], row['state']), (1, 'Suporte', 'aguardando_aprovacao'))
        status, only = await self.call('GET', self.t('/cases?phase=falha'))
        self.assertEqual([c['id'] for c in only['cases']], [failed['id']])
        self.assertGreater(data['cursor'], 0)

    async def test_the_snapshot_shows_owner_subtasks_trail_evidence_messages_and_the_concrete_approval(self):
        case = self.case()
        self.s.cases.add_note(self.ca, case['id'], 'Nota interna')
        self.s.cases.add_evidence(self.ca, case['id'], source='DNS', summary='Sem registro A', evidence_type='fato', reliability='declarada')
        proposal, _ = self.s.handoffs.delegate(self.ca, case['id'], self.other['id'], reason='Analisar', objective='Ver vencimento',
                                                expected_result='Data', summary='Dominio vencido?')
        approval = self.request_approval(case)
        data = await self.snapshot(case)
        self.assertEqual((data['case']['owner_name'], data['case']['phase']), ('Suporte', 'aguardando_humano'))
        self.assertEqual([t['assignee_name'] for t in data['tasks']], ['Financeiro'])
        self.assertEqual([(h['handoff_kind'], h['from_name'], h['to_name'], h['state']) for h in data['handoffs']],
                         [('delegacao', 'Suporte', 'Financeiro', 'proposto')])
        card = data['approvals'][0]
        self.assertEqual((card['action'], card['recipient'], card['amount_cents'], card['currency'], card['params']),
                         ('enviar_email', {'to': ['cliente@exemplo.com']}, 15000, 'brl', {'assunto': 'Cobranca'}))
        self.assertEqual(card['payload_hash'], approval['payload_hash'])
        kinds = {item['tipo'] for item in data['timeline']}
        self.assertEqual(kinds, {'entrada', 'evidencia', 'evento'})
        self.assertEqual(proposal['id'], data['handoffs'][0]['id'])
        self.assertGreater(data['cursor'], 0)
        self.assertNotIn('doc', json.dumps(data))

    async def test_other_companies_and_unknown_ids_look_the_same(self):
        mine = self.case()
        dep_b = self.s.team.create(self.cb, 'department', name='B', scope='x', status='ativo')
        theirs = self.case(ctx=self.cb, dep=dep_b, state='recebida')
        status, data = await self.call('GET', self.t('/cases'))
        self.assertEqual([c['id'] for c in data['cases']], [mine['id']])
        for path in (f'/cases/{theirs["id"]}', f'/cases/{new_id("case")}'):
            self.assertEqual(await self.call('GET', self.t(path)), (404, {'error': 'Recurso nao encontrado.'}))
        self.assertEqual((await self.call('GET', self.t(f'/events?case_id={theirs["id"]}')))[0], 404)
        for action in ('pause', 'take-over', 'reject'):
            status, _ = await self.call('POST', self.t(f'/cases/{theirs["id"]}/{action}'), {'expected_revision': 1, 'reason': 'x'})
            self.assertEqual(status, 404, action)
        status, data = await self.call('GET', self.t('/cases', self.b))
        self.assertEqual([c['id'] for c in data['cases']], [theirs['id']])


class CursorTests(CasesWebCase):
    async def test_events_resume_after_a_cursor_without_loss_or_duplicates(self):
        case = self.case()
        status, first = await self.call('GET', self.t('/events?after=0'))
        ids = [e['id'] for e in first['events']]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertGreaterEqual(len(ids), 3)
        seqs = [e['seq'] for e in first['events']]
        self.assertEqual(seqs, sorted(seqs))
        status, none = await self.call('GET', self.t(f'/events?after={first["cursor"]}'))
        self.assertEqual((none['events'], none['cursor']), ([], first['cursor']))
        self.request_approval(case)
        status, more = await self.call('GET', self.t(f'/events?after={first["cursor"]}'))
        self.assertEqual([e['type'] for e in more['events']], ['approval.requested'])
        self.assertGreater(more['cursor'], first['cursor'])
        status, replay = await self.call('GET', self.t('/events?after=0'))  # a reconnect that forgot its cursor
        self.assertEqual([e['id'] for e in replay['events']], ids + [more['events'][0]['id']])  # same ids: dedupe by id
        status, scoped = await self.call('GET', self.t(f'/events?after=0&case_id={case["id"]}'))
        self.assertTrue(all(e['case_id'] == case['id'] for e in scoped['events']))
        status, snap = await self.call('GET', self.t(f'/cases/{case["id"]}'))
        self.assertEqual(snap['cursor'], more['latest'])  # the snapshot's cursor continues the feed exactly
        self.assertEqual((await self.call('GET', self.t('/events?after=abc')))[0], 400)
        status, clamped = await self.call('GET', self.t('/events?after=-5'))
        self.assertEqual(len(clamped['events']), len(replay['events']))

    async def test_companies_have_separate_feeds(self):
        self.case()
        dep_b = self.s.team.create(self.cb, 'department', name='B', scope='x', status='ativo')
        self.case(ctx=self.cb, dep=dep_b, state='recebida')
        status, a = await self.call('GET', self.t('/events?after=0'))
        status, b = await self.call('GET', self.t('/events?after=0', self.b))
        self.assertTrue({e['id'] for e in a['events']}.isdisjoint({e['id'] for e in b['events']}))
        self.assertEqual(len(b['events']), 1)


class InterventionTests(CasesWebCase):
    async def test_approval_decisions_need_the_hash_that_was_shown(self):
        case = self.case()
        approval = self.request_approval(case)
        path = self.t(f'/approvals/{approval["id"]}')
        status, data = await self.call('POST', path + '/approve', {'shown_hash': '0' * 64})
        self.assertEqual(status, 400)
        self.assertIn('nao corresponde', data['error'])
        status, snap = await self.call('POST', path + '/approve', {'shown_hash': approval['payload_hash'], 'comment': 'Confirmado'})
        self.assertEqual(status, 200)
        self.assertEqual((snap['approvals'][0]['state'], snap['case']['phase']), ('aprovada', 'andamento'))
        status, again = await self.call('POST', path + '/approve', {'shown_hash': approval['payload_hash']})
        self.assertEqual(status, 400)
        status, snap = await self.call('POST', path + '/revoke', {'reason': 'mudei de ideia'})
        self.assertEqual(snap['approvals'][0]['state'], 'cancelada')
        second = self.request_approval(case, action='pagamento')
        status, snap = await self.call('POST', self.t(f'/approvals/{second["id"]}/deny'), {'reason': 'valor errado'})
        self.assertEqual({a['id']: a['state'] for a in snap['approvals']}[second['id']], 'negada')
        self.assertEqual((await self.call('POST', path + '/inventada', {}))[0], 404)

    async def test_two_operators_on_the_same_revision_cannot_both_win(self):
        case = self.case()
        revision = case['revision']
        status, snap = await self.call('POST', self.t(f'/cases/{case["id"]}/take-over'), {'expected_revision': revision, 'reason': 'Vou assumir'})
        self.assertEqual((status, snap['case']['operator_control'], snap['case']['phase']), (200, True, 'aguardando_humano'))
        status, data = await self.call('POST', self.t(f'/cases/{case["id"]}/pause'), {'expected_revision': revision, 'reason': 'segundo operador'})
        self.assertEqual((status, data['code']), (409, 'conflict'))
        status, returned = await self.call('POST', self.t(f'/cases/{case["id"]}/return'),
                                           {'expected_revision': snap['case']['revision'], 'reason': 'Pode seguir'})
        self.assertEqual((returned['case']['operator_control'], returned['case']['state']), (False, 'em_execucao'))
        status, missing = await self.call('POST', self.t(f'/cases/{case["id"]}/pause'), {'expected_revision': returned['case']['revision']})
        self.assertEqual(status, 400)  # a reason is required
        status, rejected = await self.call('POST', self.t(f'/cases/{case["id"]}/reject'),
                                           {'expected_revision': returned['case']['revision'], 'reason': 'Improcedente'})
        self.assertEqual((rejected['case']['state'], rejected['case']['phase']), ('cancelada', 'cancelada'))

    async def test_unknown_outcomes_are_surfaced_and_can_be_reconciled(self):
        case = self.case()
        approval = self.request_approval(case)
        self.s.approvals.approve(self.ca, approval['id'], approval['payload_hash'])
        self.s.approvals.consume(self.ca, approval['id'], approval['normalized_action'])
        self.s.approvals.record_effect(self.ca, approval['id'], 'desconhecido', 'sem resposta do provedor')
        snap = await self.snapshot(case)
        self.assertEqual((snap['case']['reconciliation'], snap['case']['phase']), (1, 'aguardando_humano'))
        status, inbox = await self.call('GET', self.t('/attention'))
        self.assertEqual(inbox['reconciliation'], [{'kind': 'approval', 'id': approval['id'], 'case_id': case['id'], 'action': 'enviar_email'}])
        status, resolved = await self.call('POST', self.t(f'/approvals/{approval["id"]}/resolve-effect'),
                                           {'outcome': 'confirmado', 'detail': 'Conferi no provedor'})
        self.assertEqual((status, resolved['case']['reconciliation'], resolved['case']['phase']), (200, 0, 'andamento'))
        self.assertEqual((await self.call('POST', self.t(f'/messages/{new_id("outbound_message")}/resolve'),
                                           {'outcome': 'nao_enviada'}))[0], 404)

    async def test_the_inbox_collects_what_needs_a_person(self):
        case = self.case()
        pending = self.request_approval(case)
        self.s.handoffs.cancel(self.ca, self.s.handoffs.delegate(self.ca, case['id'], self.other['id'], reason='r', objective='o',
                                                                   expected_result='e', summary='s')[0]['id'])
        status, inbox = await self.call('GET', self.t('/attention'))
        self.assertEqual([a['id'] for a in inbox['pending_approvals']], [pending['id']])
        self.assertEqual(inbox['reconciliation'], [])
        self.assertEqual(inbox['handoffs'], [])  # a withdrawn proposal needs nobody
        self.assertEqual((await self.call('GET', self.t('/attention', self.b)))[1]['pending_approvals'], [])

    async def test_assigning_an_owner_to_a_queued_case(self):
        case = self.case('triagem')
        status, snap = await self.call('POST', self.t(f'/cases/{case["id"]}/assign'),
                                       {'expected_revision': case['revision'], 'agent_id': self.agent['id']})
        self.assertEqual((status, snap['case']['owner_name']), (200, 'Suporte'))
        status, data = await self.call('POST', self.t(f'/cases/{case["id"]}/assign'),
                                       {'expected_revision': case['revision'], 'agent_id': self.other['id']})
        self.assertEqual(status, 409)


if __name__ == "__main__":
    unittest.main()
