import test from 'node:test';
import assert from 'node:assert/strict';
import {canCreate, canPreview, doctorHeadline, planSummary, previewMatches, sortedChecks} from '../src/team/onboardingModel.js';

const plan = {template: {id: 'atendimento-basico'}, company: {name: 'Empresa Modelo'}, plan_hash: 'a'.repeat(64),
  departments: [{}, {}, {}], agents: [{}, {}, {}], capabilities: [{}, {}], handoffs: [{}], case_access: [{}, {}, {}]};

test('a preview is only valid for the template and name it was made for', () => {
  assert.equal(previewMatches(plan, 'atendimento-basico', '  Empresa Modelo '), true);
  assert.equal(previewMatches(plan, 'somente-suporte', 'Empresa Modelo'), false);
  assert.equal(previewMatches(plan, 'atendimento-basico', 'Outra'), false);
  assert.equal(previewMatches(null, 'atendimento-basico', 'Empresa Modelo'), false);
  assert.equal(canCreate(plan, 'atendimento-basico', 'Empresa Modelo', false), true);
  assert.equal(canCreate(plan, 'atendimento-basico', 'Empresa Modelo', true), false);
  assert.equal(canCreate(plan, 'atendimento-basico', 'Mudou', false), false);
  assert.equal(canPreview('', 'x'), false);
  assert.equal(canPreview('atendimento-basico', '   '), false);
  assert.equal(canPreview('atendimento-basico', 'Empresa'), true);
});

test('the summary counts what will be created with the right plural', () => {
  assert.deepEqual(planSummary(plan), ['Empresa: Empresa Modelo', '3 departamentos', '3 agentes', '2 capacidades de triagem', '1 permissão de encaminhamento',
    '3 agentes com acesso às demandas']);
  assert.equal(planSummary({...plan, departments: [{}], agents: [{}]})[1], '1 departamento');
});

test('the installation check shows what must be fixed first and says so in one line', () => {
  const report = {itens: [{id: 'a', estado: 'ok'}, {id: 'b', estado: 'aviso'}, {id: 'c', estado: 'erro'}, {id: 'd', estado: 'ok'}]};
  assert.deepEqual(sortedChecks(report).map(i => i.id), ['c', 'b', 'a', 'd']);
  assert.equal(doctorHeadline(report), '1 problema(s) a corrigir e 1 aviso(s).');
  assert.equal(doctorHeadline({itens: [{estado: 'aviso'}, {estado: 'ok'}]}), 'Sem problemas; 1 aviso(s) para ver.');
  assert.equal(doctorHeadline({itens: [{estado: 'ok'}]}), 'Tudo certo com a instalação.');
});
