const {app, BrowserWindow, ipcMain, session, screen, shell} = require('electron');
const path = require('node:path');
const fs = require('node:fs');
const {pathToFileURL} = require('node:url');
const {BASE_URL, trustedChat, sanitizeStatus, audioPermission, externalAuthUrl} = require('./policy.cjs');
const {BackendRuntime}=require('./runtime.cjs');
if(!app.isPackaged) app.setPath('userData',path.join(__dirname,'..','.runtime','desktop'));
// Small 2D windows do not need to compete with local inference for GPU memory.
app.disableHardwareAcceleration();
if(process.argv.includes('--smoke-test')) setTimeout(()=>{console.error('Desktop smoke test timed out.'); app.exit(1);},45000).unref();

let chat, avatar, quitting = false, runtime, starting=false, cleaned=false;
let status = {state:'connecting', micEnabled:false, level:0, waitingHuman:false, caseRef:null, caseLabel:null, agentName:null};
let runtimeStatus={stage:'starting',message:'Verificando a instalação…'};
const avatarPage = pathToFileURL(path.join(__dirname,'avatar.html')).href;
const startupPage=pathToFileURL(path.join(__dirname,'startup.html')).href;
const showChat = () => {if(chat && !chat.isDestroyed()) {chat.show(); chat.restore(); chat.focus();}};

async function bootstrap() {
  if(starting||quitting)return;
  starting=true;
  try {
    await runtime.start();
    if(!quitting)await chat.loadURL(BASE_URL);
  }catch(error){
    await runtime.stop();
    runtimeStatus={stage:'error',message:error.message};
    if(!quitting){chat.webContents.send('runtime:status',runtimeStatus);showChat();status={state:'error',micEnabled:false,level:0};avatar.webContents.send('avatar:status',status);}
  }finally{starting=false;}
}

function secureWindow(win, allowed) {
  win.webContents.setWindowOpenHandler(() => ({action:'deny'}));
  win.webContents.on('will-navigate', (event,url) => {if(!allowed(url)) event.preventDefault();});
  win.webContents.on('will-attach-webview', event => event.preventDefault());
}

async function createWindows() {
  const prefs = {preload:path.join(__dirname,'preload.cjs'), nodeIntegration:false, contextIsolation:true, sandbox:true, backgroundThrottling:false};
  chat = new BrowserWindow({width:1120,height:800,minWidth:680,minHeight:600,title:'Assistente',backgroundColor:'#181a21',webPreferences:prefs});
  chat.setMenuBarVisibility(false);
  secureWindow(chat,url=>trustedChat(url)||url===startupPage);
  chat.on('close', event => {if(!quitting) {event.preventDefault(); chat.hide();}});
  chat.webContents.on('render-process-gone', () => {status={state:'offline',micEnabled:false,level:0}; avatar?.webContents.send('avatar:status',status);});
  const area = screen.getPrimaryDisplay().workArea;
  avatar = new BrowserWindow({width:240,height:350,x:area.x+area.width-260,y:area.y+area.height-380,resizable:false,frame:false,backgroundColor:'#19231e',alwaysOnTop:true,title:'Assistente · Avatar',webPreferences:prefs});
  avatar.setAlwaysOnTop(true,'screen-saver');
  secureWindow(avatar,url=>url===avatarPage);
  avatar.on('closed',()=>{if(!quitting) app.quit();});
  avatar.webContents.on('did-finish-load',()=>avatar.webContents.send('avatar:status',status));
  await avatar.loadFile(path.join(__dirname,'avatar.html'));
  avatar.show();
  avatar.focus();
  avatar.setAlwaysOnTop(true,'screen-saver');
  await chat.loadFile(path.join(__dirname,'startup.html'));
  const root=app.isPackaged?path.join(process.resourcesPath,'backend'):path.join(__dirname,'..');
  runtime=new BackendRuntime({root,dataDir:process.env.ASSISTENTE_DATA_DIR||(app.isPackaged?path.join(app.getPath('userData'),'data'):root),
    uvPath:app.isPackaged?path.join(process.resourcesPath,'tools','uv.exe'):(fs.existsSync(path.join(process.env.USERPROFILE||'','.local','bin','uv.exe'))?path.join(process.env.USERPROFILE||'','.local','bin','uv.exe'):'uv'),
    onProgress:progress=>{runtimeStatus=progress;if(chat&&!chat.isDestroyed())chat.webContents.send('runtime:status',progress);}});
  if(!process.argv.includes('--smoke-test'))void bootstrap();
}

ipcMain.on('assistant:status',(event,value)=>{
  if(event.sender!==chat?.webContents || !trustedChat(event.senderFrame?.url)) return;
  const next=sanitizeStatus(value); if(!next) return;
  status=next; if(avatar && !avatar.isDestroyed()) avatar.webContents.send('avatar:status',status);
});
ipcMain.on('integration:open-external',(event,url)=>{
  if(event.sender!==chat?.webContents || !trustedChat(event.senderFrame?.url)) return;
  const safe=externalAuthUrl(url); if(safe) void shell.openExternal(safe);
});
ipcMain.on('avatar:action',(event,action)=>{
  if(event.sender!==avatar?.webContents || event.senderFrame?.url!==avatarPage) return;
  if(action==='open-chat') showChat();
  if(action==='toggle-mic') {if(trustedChat(chat?.webContents.getURL())) chat.webContents.send('assistant:command','toggle-mic'); else showChat();}
  // The avatar never supplies ids: the case comes from the last status the trusted chat published and was validated.
  if(action==='open-case') {showChat(); if(status.caseRef && trustedChat(chat?.webContents.getURL())) chat.webContents.send('assistant:command',{type:'open-case',ref:status.caseRef});}
  if(action==='minimize') avatar.minimize();
  if(action==='quit') app.quit();
});
ipcMain.on('runtime:action',(event,action)=>{
  if(event.sender!==chat?.webContents||event.senderFrame?.url!==startupPage)return;
  if(action==='status')chat.webContents.send('runtime:status',runtimeStatus);
  if(action==='retry')void bootstrap();
  if(action==='folder'&&runtime)void shell.openPath(runtime.dataDir);
  if(action==='ollama')void shell.openExternal('https://ollama.com/download/windows');
  if(action==='quit')app.quit();
});

if(!app.requestSingleInstanceLock()) app.quit();
else {
  app.on('second-instance',()=>{showChat(); if(avatar) {avatar.restore(); avatar.show();}});
  app.whenReady().then(async()=>{
    app.setAppUserModelId('br.assistente.local');
    session.defaultSession.setPermissionRequestHandler((contents,permission,callback,details)=>{
      callback(contents===chat?.webContents && permission==='media' && audioPermission(details.requestingUrl || contents.getURL(),details.mediaTypes));
    });
    session.defaultSession.setPermissionCheckHandler((contents,permission,origin,details)=>
      contents===chat?.webContents && permission==='media' && trustedChat(contents.getURL()) && details.mediaType==='audio');
    session.defaultSession.on('will-download',event=>event.preventDefault());
    await createWindows();
    if(process.argv.includes('--smoke-test')) {
      const assert=require('node:assert/strict');
      await new Promise(resolve=>setTimeout(resolve,200));
      assert.equal(avatar.isAlwaysOnTop(),true,'avatar always-on-top');
      for(const win of [chat,avatar]) {
        const prefs=win.webContents.getLastWebPreferences();
        assert.equal(prefs.nodeIntegration,false,'nodeIntegration'); assert.equal(prefs.contextIsolation,true,'contextIsolation'); assert.equal(prefs.sandbox,true,'sandbox');
      }
      console.log('PASS: avatar always-on-top; sandbox/contextIsolation; nodeIntegration disabled.');
      app.quit();
    }
  }).catch(error=>{console.error(error.message); app.exit(1);});
}
app.on('before-quit',event=>{
  quitting=true;
  if(runtime&&!cleaned){
    event.preventDefault();cleaned=true;
    // Destroy renderers first to stop capture and close the AgentSession.
    for(const win of [chat,avatar])if(win&&!win.isDestroyed())win.destroy();
    Promise.race([runtime.stop(),new Promise(resolve=>setTimeout(resolve,8000))]).finally(()=>app.exit(0));
  }
});
app.on('window-all-closed',()=>app.quit());

