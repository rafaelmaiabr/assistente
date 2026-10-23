const labels={connecting:'Conectando…',offline:'Sem conexão',error:'Confira o aviso no chat',ready:'Pronto para ajudar',listening:'Estou ouvindo',processing:'Pensando…',executing:'Executando ação…',transcribing:'Entendendo sua fala…',synthesizing:'Preparando áudio…',speaking:'Respondendo…'};
const root=document.querySelector('.pet'), mic=document.getElementById('mic');
window.desktopBridge.onStatus(status=>{
  root.dataset.state=status.state;
  document.getElementById('state').textContent=labels[status.state] || 'Aguardando';
  document.getElementById('level').value=status.level;
  document.getElementById('capture').textContent=status.micEnabled?'Captura ativa':'Microfone desligado';
  mic.textContent=status.micEnabled?'Silenciar mic':'Ligar mic'; mic.setAttribute('aria-pressed',String(status.micEnabled));
  mic.disabled=['offline','connecting'].includes(status.state)||(status.state==='error'&&!status.micEnabled);
  const human=document.getElementById('human'),caseLine=document.getElementById('case'),openCase=document.getElementById('open-case');
  human.hidden=!(status.waitingHuman&&status.caseRef);
  caseLine.hidden=!status.caseRef; caseLine.textContent=status.caseRef?[status.agentName,status.caseLabel].filter(Boolean).join(' · '):'';
  openCase.hidden=!status.caseRef;
  root.dataset.human=String(Boolean(status.waitingHuman&&status.caseRef));
});
for(const [id,action] of Object.entries({mic:'toggle-mic',chat:'open-chat',quit:'quit',minimize:'minimize','open-case':'open-case'})) document.getElementById(id).addEventListener('click',()=>window.desktopBridge.avatarAction(action));
