from tests.test_team_cases_web import CasesWebCase

REQUESTED = 'Consultar o registro de um dominio na Cloudflare'


class GapsWebTests(CasesWebCase):
    async def test_the_operator_reports_lists_decides_and_fulfills_through_the_api(self):
        first, second = self.case(), self.case()
        status, gap = await self.call('POST', self.t('/gaps'), {'case_id': first['id'], 'requested': REQUESTED, 'topic': 'dominio cloudflare',
                                                                   'tried': ['skill de diagnostico nao cobre a Cloudflare'], 'gap_type': 'ferramenta'})
        self.assertEqual((status, gap['state'], gap['occurrences']), (201, 'aberta', 1))
        status, again = await self.call('POST', self.t('/gaps'), {'case_id': second['id'], 'requested': REQUESTED, 'topic': 'dominio cloudflare'})
        self.assertEqual((again['id'], again['occurrences']), (gap['id'], 2))
        snapshot = await self.snapshot(first)
        self.assertEqual((snapshot['case']['open_gaps'], snapshot['case']['phase'], snapshot['gaps'][0]['id']), (1, 'aguardando_humano', gap['id']))
        status, inbox = await self.call('GET', self.t('/attention'))
        self.assertEqual([(g['id'], g['occurrences']) for g in inbox['gaps']], [(gap['id'], 2)])
        status, listed = await self.call('GET', self.t('/gaps?state=aberta'))
        self.assertEqual([g['id'] for g in listed['gaps']], [gap['id']])
        status, body = await self.call('POST', self.t(f'/gaps/{gap["id"]}/decide'),
                                       {'expected_revision': 1, 'decision': 'criar_skill', 'note': 'Vale a pena'})
        self.assertEqual(status, 409, body)  # the second report moved the revision
        status, decided = await self.call('POST', self.t(f'/gaps/{gap["id"]}/decide'),
                                          {'expected_revision': again['revision'], 'decision': 'criar_skill', 'note': 'Vale a pena',
                                           'follow_up': 'issue 99'})
        self.assertEqual((status, decided['state'], decided['follow_up']), (200, 'decidida', 'issue 99'))
        status, done = await self.call('POST', self.t(f'/gaps/{gap["id"]}/fulfill'),
                                       {'expected_revision': decided['revision'], 'note': 'Skill publicada'})
        self.assertEqual((status, done['state']), (200, 'atendida'))
        self.assertEqual((await self.snapshot(first))['case']['state'], 'em_execucao')

    async def test_declining_cancels_and_validation_errors_are_short_sentences(self):
        case = self.case()
        status, gap = await self.call('POST', self.t('/gaps'), {'case_id': case['id'], 'requested': REQUESTED})
        status, body = await self.call('POST', self.t(f'/gaps/{gap["id"]}/decide'), {'expected_revision': gap['revision'], 'decision': 'nao_atender', 'note': ' '})
        self.assertEqual(status, 400)
        self.assertIn('justificativa', body['error'])
        status, decided = await self.call('POST', self.t(f'/gaps/{gap["id"]}/decide'),
                                          {'expected_revision': gap['revision'], 'decision': 'nao_atender', 'note': 'Fora do servico'})
        self.assertEqual((status, decided['decision']), (200, 'nao_atender'))
        self.assertEqual((await self.snapshot(case))['case']['state'], 'cancelada')
        status, body = await self.call('POST', self.t('/gaps'), {'case_id': case['id'], 'requested': REQUESTED, 'extra': 1})
        self.assertEqual(status, 400)
        status, body = await self.call('POST', self.t(f'/gaps/{gap["id"]}/inventar'), {})
        self.assertEqual(status, 404)

    async def test_another_company_never_sees_or_decides_a_gap(self):
        case = self.case()
        status, gap = await self.call('POST', self.t('/gaps'), {'case_id': case['id'], 'requested': REQUESTED})
        status, listed = await self.call('GET', self.t('/gaps', self.b))
        self.assertEqual(listed['gaps'], [])
        status, body = await self.call('POST', self.t(f'/gaps/{gap["id"]}/decide', self.b),
                                       {'expected_revision': 1, 'decision': 'criar_skill', 'note': 'x'})
        self.assertEqual(status, 404)
        status, body = await self.call('POST', self.t('/gaps', self.b), {'case_id': case['id'], 'requested': REQUESTED})
        self.assertEqual(status, 404)
