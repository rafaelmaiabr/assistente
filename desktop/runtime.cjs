const fs=require('node:fs'), fsp=require('node:fs/promises'),path=require('node:path');
const os=require('node:os'),crypto=require('node:crypto');
const {spawn}=require('node:child_process');
const {BASE_URL}=require('./policy.cjs');

async function jsonAt(url,timeout=2500) {
  const response=await fetch(url,{signal:AbortSignal.timeout(timeout)});
  if(!response.ok) throw new Error('HTTP '+response.status);
  return response.json();
}

class BackendRuntime {
  constructor({root,dataDir,uvPath,onProgress=()=>{},spawnProcess=spawn,readJson=jsonAt}) {
    Object.assign(this,{root,dataDir,uvPath,onProgress,spawnProcess,readJson});
    this.children=new Set(); this.stopped=false;
  }
  progress(stage,message) {this.onProgress({stage,message});}
  checkRunning() {if(this.stopped) throw new Error('Inicialização cancelada.');}
  async run(file,args,env={},background=false) {
    this.checkRunning();
    const child=this.spawnProcess(file,args,{cwd:this.root,env:{...process.env,...env},windowsHide:true,stdio:'ignore'});
    this.children.add(child);
    child.once('exit',()=>this.children.delete(child));
    if(background) {
      await new Promise((resolve,reject)=>{child.once('spawn',resolve);child.once('error',reject);});
      return child;
    }
    await new Promise((resolve,reject)=>{child.once('error',reject);child.once('exit',code=>code===0?resolve():reject(new Error('Não foi possível preparar o ambiente Python. Confira internet e espaço em disco e tente novamente.')));});
  }
  async preparePython() {
    const python=path.join(this.dataDir,'.venv','Scripts','python.exe');
    const requirement=await fsp.readFile(path.join(this.root,'requirements.txt'));
    const hash=crypto.createHash('sha256').update(requirement).digest('hex');
    const stamp=path.join(this.dataDir,'.runtime','dependencies.sha256');
    let current='';try{current=await fsp.readFile(stamp,'utf8');}catch{}
    if(fs.existsSync(python) && current===hash) return python;
    // An existing checkout venv is audited as well; never trust only the filename.
    this.progress('python','Preparando Python e verificando dependências… Na primeira vez é necessária internet.');
    const env={UV_CACHE_DIR:path.join(this.dataDir,'.runtime','uv-cache'),UV_PYTHON_INSTALL_DIR:path.join(this.dataDir,'.runtime','python')};
    if(!fs.existsSync(python)) await this.run(this.uvPath,['venv','--python','3.13',path.join(this.dataDir,'.venv')],env);
    await this.run(this.uvPath,['pip','install','--python',python,'-r',path.join(this.root,'requirements.txt')],env);
    await fsp.mkdir(path.dirname(stamp),{recursive:true}); await fsp.writeFile(stamp,hash);
    return python;
  }
  async waitFor(url,validate,seconds=60) {
    for(let attempt=0;attempt<seconds*2;attempt++) {
      this.checkRunning();
      try {const data=await this.readJson(url);if(validate(data)) return data;} catch {}
      await new Promise(resolve=>setTimeout(resolve,500));
    }
    throw new Error('O serviço local não iniciou a tempo. Tente novamente ou confira se a porta está ocupada.');
  }
  async start() {
    this.stopped=false;
    if(!fs.existsSync(path.join(this.root,'frontend','dist','index.html')))throw new Error('Interface não compilada. Abra iniciar-desktop.cmd ou utilize o pacote portable completo.');
    await fsp.mkdir(this.dataDir,{recursive:true});
    const local=path.join(this.dataDir,'config.json');
    if(!fs.existsSync(local)) await fsp.copyFile(path.join(this.root,'config.example.json'),local);
    let config;
    try {config={...JSON.parse(await fsp.readFile(path.join(this.root,'config.example.json'),'utf8')),...JSON.parse((await fsp.readFile(local,'utf8')).replace(/^\uFEFF/,''))};}
    catch {throw new Error('config.json inválido. Abra a pasta de configuração, corrija o JSON e tente novamente.');}
    let origin;
    try {const url=new URL(config.ollama_url);if(!['http:','https:'].includes(url.protocol))throw 0;origin=url.origin;}
    catch {throw new Error('Configure um endereço HTTP válido para o Ollama em config.json.');}
    this.progress('ollama','Verificando Ollama e modelo local…');
    let tags;
    try {tags=await this.readJson(origin+'/api/tags');}catch {
      const url=new URL(origin);
      if(!['127.0.0.1','localhost'].includes(url.hostname))throw new Error('O provedor configurado está indisponível. Inicie-o e tente novamente.');
      const candidates=[path.join(process.env.LOCALAPPDATA||'', 'Programs','Ollama','ollama.exe'), 'ollama'];
      const executable=candidates.find(candidate=>candidate==='ollama'||fs.existsSync(candidate));
      const env={OLLAMA_HOST:'127.0.0.1:'+url.port};
      const homeModels=path.join(os.homedir(),'.ollama','models');
      if(fs.existsSync(path.join(homeModels,'manifests')))env.OLLAMA_MODELS=homeModels;
      // Scope GPU preference to the service created by this application.
      env.OLLAMA_NO_GPU='';
      try {await this.run(executable,['serve'],env,true);}
      catch {throw new Error('Ollama não encontrado. Instale o Ollama, disponibilize o modelo configurado e tente novamente.');}
      tags=await this.waitFor(origin+'/api/tags',value=>Array.isArray(value.models));
    }
    if(!Array.isArray(tags.models)||!tags.models.some(model=>model.name===config.model||model.model===config.model)) {
      const names=(tags.models||[]).map(model=>model.name).filter(Boolean).slice(0,8);
      throw new Error('Modelo '+config.model+' não instalado. Escolha um modelo existente em config.json'+(names.length?': '+names.join(', '):' ou instale um modelo no Ollama')+'.');
    }
    this.checkRunning();
    let existing;
    try {existing=await this.readJson(BASE_URL+'/api/health');}catch{}
    if(existing) {
      if(existing.app!=='assistente'||existing.version!==3)throw new Error('A porta 8765 está ocupada por outra versão. Encerre o servidor anterior antes de continuar.');
      this.progress('ready','Assistente pronto. Servidor local existente reutilizado.');return {borrowed:true};
    }
    const python=await this.preparePython();
    this.progress('starting','Iniciando o assistente local…');
    await this.run(python,[path.join(this.root,'web_server.py')],{ASSISTENTE_DATA_DIR:this.dataDir},true);
    await this.waitFor(BASE_URL+'/api/health',value=>value.app==='assistente'&&value.version===3);
    this.progress('ready','Assistente pronto.');return {borrowed:false};
  }
  async stop() {
    this.stopped=true;
    const owned=[...this.children];this.children.clear();
    await Promise.all(owned.map(child=>new Promise(resolve=>{
      if(!child.pid||child.exitCode!==null)return resolve();
      if(process.platform==='win32') {
        const killer=this.spawnProcess('taskkill.exe',['/PID',String(child.pid),'/T','/F'],{windowsHide:true,stdio:'ignore'});
        killer.once('error',resolve);killer.once('exit',resolve);
      } else {child.kill();resolve();}
    })));
  }
}
module.exports={BackendRuntime,jsonAt};
