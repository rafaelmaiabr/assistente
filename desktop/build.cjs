const fs=require('node:fs/promises'),path=require('node:path'),{spawnSync}=require('node:child_process');
const BACKEND_FILES=['agent.py','assistant_core.py','honesty.py','mail_intent.py','chat_context.py','tool_guard.py','persona.py','person_memory.py','memory_intent.py','personal_mail.py','personal_outbox.py','browser_agent.py','web_server.py','speech_service.py','voice_profiles.py','voice_engine.py','local_audio.py','settings.py','demo_mcp.py','config.example.json','requirements.txt'];
const BACKEND_DIRS=['team'];
// The domain package is an allowlist by extension: anything else found inside it stops the build instead of shipping silently.
const ALLOWED_EXTENSIONS=new Set(['.py','.sql','.json','.md']);
const PRIVATE_NAMES=/^(config\.json|persona\.txt|memoria-pessoal\.json|voice-settings\.json|validacao\.json|operator\.json|persistence\.json|retention\.json|launchers\.json|\.env.*)$|\.(sqlite3?|db|bak|log|wav|mp3|gguf|bin|pem|key)$/i;
async function backendInventory(root){
  const found=[];
  async function walk(dir){
    for(const entry of await fs.readdir(path.join(root,dir),{withFileTypes:true})){
      const relative=path.posix.join(dir.split(path.sep).join('/'),entry.name);
      if(entry.isDirectory()){if(entry.name!=='__pycache__')await walk(relative);continue;}
      if(!entry.isFile()||PRIVATE_NAMES.test(entry.name)||!ALLOWED_EXTENSIONS.has(path.extname(entry.name).toLowerCase()))
        throw new Error('Arquivo inesperado no pacote do backend: '+relative+'. Remova-o ou inclua-o conscientemente na lista de extensões permitidas.');
      found.push(relative);
    }
  }
  for(const dir of BACKEND_DIRS)await walk(dir);
  return found.sort();
}
const DESKTOP_FILES=['main.cjs','runtime.cjs','policy.cjs','preload.cjs','avatar.html','avatar.css','avatar.js','startup.html','startup.js','offline.css'];
async function build() {
  if(process.platform!=='win32')throw new Error('Crie o pacote Windows em um host Windows.');
  const root=path.resolve(__dirname,'..'),stage=path.join(root,'.tmp','package-stage');
  const appDir=path.join(stage,'app'),backend=path.join(stage,'backend'),tools=path.join(stage,'tools');
  // Only remove a fixed staging directory inside this checkout.
  if(!stage.startsWith(root+path.sep)||path.basename(stage)!=='package-stage')throw new Error('Pasta de staging inválida.');
  await fs.rm(stage,{recursive:true,force:true});
  for(const dir of [appDir,backend,tools])await fs.mkdir(dir,{recursive:true});
  for(const file of DESKTOP_FILES)await fs.copyFile(path.join(__dirname,file),path.join(appDir,file));
  const version=require('./package.json').version;
  await fs.writeFile(path.join(appDir,'package.json'),JSON.stringify({name:'assistente-local',version,main:'main.cjs',description:'Assistente local de voz e texto',author:'Rafael'},null,2));
  for(const file of BACKEND_FILES)await fs.copyFile(path.join(root,file),path.join(backend,file));
  // Packages are copied file by file from an inventory that rejects anything private or unexpected (see backendInventory).
  const inventory=await backendInventory(root);
  for(const file of inventory){await fs.mkdir(path.dirname(path.join(backend,file)),{recursive:true});await fs.copyFile(path.join(root,file),path.join(backend,file));}
  await fs.access(path.join(root,'frontend','dist','index.html'));
  await fs.cp(path.join(root,'frontend','dist'),path.join(backend,'frontend','dist'),{recursive:true});
  const located=spawnSync('where.exe',['uv'],{encoding:'utf8',windowsHide:true});
  const uv=located.stdout?.trim().split(/\r?\n/)[0];
  if(!uv)throw new Error('uv não encontrado no computador de build.');
  await fs.copyFile(uv,path.join(tools,'uv.exe'));
  await fs.copyFile(path.join(__dirname,'licenses','uv-LICENSE-MIT.txt'),path.join(tools,'LICENSE-MIT.txt'));
  const uvVersion=spawnSync(uv,['--version'],{encoding:'utf8',windowsHide:true}).stdout.trim();
  const migrations=inventory.filter(file=>file.startsWith('team/migrations/')).map(file=>path.posix.basename(file));
  const manifest={version,electron:require('electron/package.json').version,uv:uvVersion,backendFiles:BACKEND_FILES,backendDirs:BACKEND_DIRS,backendFileCount:inventory.length,migrations,
    templates:inventory.filter(file=>file.startsWith('team/templates/')).map(file=>path.posix.basename(file)),containsModels:false,containsLocalConfig:false,containsUserData:false};
  await fs.writeFile(path.join(backend,'build-manifest.json'),JSON.stringify(manifest,null,2));
  const {packager}=await import('@electron/packager');
  const paths=await packager({dir:appDir,name:'Assistente',executableName:'Assistente',platform:'win32',arch:'x64',
    electronVersion:manifest.electron,out:path.join(root,'release'),overwrite:true,prune:false,asar:false,
    extraResource:[backend,tools],appVersion:version,win32metadata:{CompanyName:'Assistente Local',FileDescription:'Assistente Local',ProductName:'Assistente'}});
  console.log('Pacote criado: '+paths[0]);
}
if(require.main===module)build().catch(error=>{console.error(error.message);process.exitCode=1;});
module.exports={BACKEND_FILES,BACKEND_DIRS,DESKTOP_FILES,ALLOWED_EXTENSIONS,PRIVATE_NAMES,backendInventory};
