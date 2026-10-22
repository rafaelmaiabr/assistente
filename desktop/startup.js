const title=document.getElementById('title'),message=document.getElementById('message'),retry=document.getElementById('retry');
window.desktopBridge.onRuntime(state=>{
  title.textContent=state.stage==='error'?'Vamos ajustar a instalação':'Preparando seu assistente';
  message.textContent=state.message;retry.disabled=state.stage!=='error';
});
for(const id of ['retry','folder','ollama','quit'])document.getElementById(id).addEventListener('click',()=>window.desktopBridge.runtimeAction(id));
window.desktopBridge.runtimeAction('status');
