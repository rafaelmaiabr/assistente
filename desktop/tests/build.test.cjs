const test=require('node:test'),assert=require('node:assert/strict');
const fs=require('node:fs'),path=require('node:path');
const os=require('node:os');
const {BACKEND_FILES,BACKEND_DIRS,PRIVATE_NAMES,ALLOWED_EXTENSIONS,backendInventory}=require('../build.cjs');
const root=path.resolve(__dirname,'..','..');
test('package allowlist points at existing backend files and domain packages',()=>{
  for(const file of BACKEND_FILES)assert.ok(fs.statSync(path.join(root,file)).isFile(),file);
  assert.ok(BACKEND_DIRS.includes('team'));
  assert.ok(fs.statSync(path.join(root,'team','migrations','0001_initial.sql')).isFile());
  for(const dir of BACKEND_DIRS)assert.ok(fs.statSync(path.join(root,dir,'__init__.py')).isFile(),dir);
});
test('allowlist excludes local configuration and data',()=>{
  for(const forbidden of ['config.json','voice-settings.json','validacao.json'])assert.ok(!BACKEND_FILES.includes(forbidden));
});

test('the package carries every migration, schema, template and demo skill of the team domain',async()=>{
  const inventory=await backendInventory(root);
  const migrations=fs.readdirSync(path.join(root,'team','migrations')).filter(name=>name.endsWith('.sql'));
  assert.ok(migrations.length>=22);
  for(const name of migrations)assert.ok(inventory.includes('team/migrations/'+name),name);
  for(const needed of ['team/app.py','team/identity.py','team/remote.py','team/onboarding.py','team/doctor.py','team/gaps.py','team/templates/atendimento-basico.json',
    'team/schemas/case.schema.json','team/skills_demo/diagnostico-dominio/instrucoes.md','team/examples/team-bundle.json'])assert.ok(inventory.includes(needed),needed);
});
test('the package never carries tests, evals, scripts, caches, data, databases, logs, audio, models or credentials',async()=>{
  const inventory=await backendInventory(root);
  assert.ok(inventory.length>100);
  for(const file of inventory){
    assert.ok(file.startsWith('team/'),file);
    assert.ok(!file.includes('__pycache__'),file);
    assert.ok(ALLOWED_EXTENSIONS.has(path.extname(file).toLowerCase()),file);
    assert.ok(!PRIVATE_NAMES.test(path.basename(file)),file);
  }
  for(const folder of ['tests/','evals/','scripts/','graphify-out/','team-data/','.tmp/','release/'])assert.ok(!inventory.some(file=>file.startsWith(folder)),folder);
});
test('anything private or unexpected inside the domain package stops the build instead of being shipped',async()=>{
  const folder=fs.mkdtempSync(path.join(os.tmpdir(),'pacote-'));
  try{
    fs.mkdirSync(path.join(folder,'team'));
    fs.writeFileSync(path.join(folder,'team','__init__.py'),'');
    fs.writeFileSync(path.join(folder,'team','ok.json'),'{}');
    assert.deepEqual(await backendInventory(folder),['team/__init__.py','team/ok.json']);
    for(const stray of ['team.sqlite3','config.json','notas.txt','chave.pem','conversa.log','voz.wav','modelo.gguf','.env','segredo.key','operator.json']){
      fs.writeFileSync(path.join(folder,'team',stray),'x');
      await assert.rejects(backendInventory(folder),/Arquivo inesperado/,stray);
      fs.rmSync(path.join(folder,'team',stray));
    }
    fs.mkdirSync(path.join(folder,'team','__pycache__'));
    fs.writeFileSync(path.join(folder,'team','__pycache__','x.cpython-313.pyc'),'x');
    assert.deepEqual(await backendInventory(folder),['team/__init__.py','team/ok.json']);   // caches are skipped, not an error
  }finally{fs.rmSync(folder,{recursive:true,force:true});}
});
test('the backend files in the package import nothing that the package leaves out',()=>{
  const packaged=new Set(BACKEND_FILES.map(file=>file.replace(/\.py$/,'')).concat(BACKEND_DIRS));
  for(const file of BACKEND_FILES.filter(name=>name.endsWith('.py'))){
    const source=fs.readFileSync(path.join(root,file),'utf8');
    for(const match of source.matchAll(/^(?:from|import)\s+(evals|scripts|tests)\b/gm))assert.fail(file+' importa '+match[1]);
  }
  for(const file of fs.readdirSync(path.join(root,'team')).filter(name=>name.endsWith('.py'))){
    const source=fs.readFileSync(path.join(root,'team',file),'utf8');
    for(const match of source.matchAll(/^(?:from|import)\s+(evals|scripts|tests)\b/gm))assert.fail('team/'+file+' importa '+match[1]);
  }
  assert.ok(packaged.has('team')&&packaged.has('web_server')&&packaged.has('settings'));
});
test('every root module that a packaged file imports is packaged too',()=>{
  const roots=new Set(fs.readdirSync(root).filter(name=>name.endsWith('.py')).map(name=>name.replace(/\.py$/,'')));
  const packaged=new Set(BACKEND_FILES.map(file=>file.replace(/\.py$/,'')));
  const sources=BACKEND_FILES.filter(name=>name.endsWith('.py')).map(file=>[file,path.join(root,file)])
    .concat(fs.readdirSync(path.join(root,'team')).filter(name=>name.endsWith('.py')).map(file=>['team/'+file,path.join(root,'team',file)]));
  for(const [name,full] of sources){
    for(const match of fs.readFileSync(full,'utf8').matchAll(/^(?:from|import)\s+([A-Za-z_]\w*)/gm)){
      if(roots.has(match[1])&&!packaged.has(match[1]))assert.fail(name+' importa '+match[1]+', que o pacote deixa de fora');
    }
  }
});
