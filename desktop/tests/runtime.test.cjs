const test=require('node:test'),assert=require('node:assert/strict');
const fs=require('node:fs/promises'),path=require('node:path'),os=require('node:os');
const {EventEmitter}=require('node:events');
const {BackendRuntime}=require('../runtime.cjs');

async function fixture(t,readJson) {
  const root=await fs.mkdtemp(path.join(os.tmpdir(),'assistente-'));
  t.after(()=>fs.rm(root,{recursive:true,force:true}));
  const config={model:'demo',ollama_url:'http://127.0.0.1:11435/v1'};
  await fs.writeFile(path.join(root,'config.example.json'),JSON.stringify(config));
  await fs.mkdir(path.join(root,'frontend','dist'),{recursive:true});
  await fs.writeFile(path.join(root,'frontend','dist','index.html'),'test');
  return new BackendRuntime({root,dataDir:root,uvPath:'uv',readJson});
}
test('reuse of a running server never spawns or kills it',async t=>{
  const runtime=await fixture(t,async url=>url.includes('tags')?{models:[{name:'demo'}]}:{app:'assistente',version:3});
  runtime.spawnProcess=()=>{throw new Error('must not spawn');};
  assert.deepEqual(await runtime.start(),{borrowed:true});
  await runtime.stop();assert.equal(runtime.children.size,0);
});
test('missing model and invalid config produce actionable diagnostics',async t=>{
  const runtime=await fixture(t,async()=>({models:[{name:'other'}]}));
  await assert.rejects(runtime.start(),/Modelo demo não instalado/);
  await fs.writeFile(path.join(runtime.dataDir,'config.json'),'{');
  await assert.rejects(runtime.start(),/config.json inválido/);
});
test('cleanup targets only processes created by this runtime',async t=>{
  const runtime=await fixture(t,async()=>({}));const spawned=[];
  runtime.spawnProcess=(file,args)=>{spawned.push({file,args});const child=new EventEmitter();child.pid=123;child.exitCode=null;child.kill=()=>{};setImmediate(()=>{child.emit('spawn');if(file==='taskkill.exe')child.emit('exit',0);});return child;};
  await runtime.run('owned.exe',[],{},true);await runtime.stop();
  if(process.platform==='win32')assert.deepEqual(spawned[1].args,['/PID','123','/T','/F']);
  assert.equal(runtime.children.size,0);
});
