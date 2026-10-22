const {app, BrowserWindow, ipcMain, session, screen} = require('electron');
const path = require('node:path');
const {pathToFileURL} = require('node:url');
const {BASE_URL, trustedChat, sanitizeStatus, audioPermission} = require('./policy.cjs');
if(!app.isPackaged) app.setPath('userData',path.join(__dirname,'..','.runtime','desktop'));
// Small 2D windows do not need to compete with local inference for GPU memory.
app.disableHardwareAcceleration();
if(process.argv.includes('--smoke-test')) setTimeout(()=>{console.error('Desktop smoke test timed out.'); app.exit(1);},45000).unref();

let chat, avatar, quitting = false;
let status = {state:'connecting', micEnabled:false, level:0};
const avatarPage = pathToFileURL(path.join(__dirname,'avatar.html')).href;
const showChat = () => {if(chat && !chat.isDestroyed()) {chat.show(); chat.restore(); chat.focus();}};

function secureWindow(win, allowed) {
  win.webContents.setWindowOpenHandler(() => ({action:'deny'}));
  win.webContents.on('will-navigate', (event,url) => {if(!allowed(url)) event.preventDefault();});
  win.webContents.on('will-attach-webview', event => event.preventDefault());
}

async function createWindows() {
  const prefs = {preload:path.join(__dirname,'preload.cjs'), nodeIntegration:false, contextIsolation:true, sandbox:true, backgroundThrottling:false};
  chat = new BrowserWindow({width:1120,height:800,minWidth:680,minHeight:600,title:'Assistente',backgroundColor:'#181a21',webPreferences:prefs});
  chat.setMenuBarVisibility(false);
  secureWindow(chat,trustedChat);
  chat.on('close', event => {if(!quitting) {event.preventDefault(); chat.hide();}});
  chat.webContents.on('render-process-gone', () => {status={state:'offline',micEnabled:false,level:0}; avatar?.webContents.send('avatar:status',status);});
  const area = screen.getPrimaryDisplay().workArea;
  avatar = new BrowserWindow({width:240,height:310,x:area.x+area.width-260,y:area.y+area.height-340,resizable:false,frame:false,backgroundColor:'#19231e',alwaysOnTop:true,title:'Assistente · Avatar',webPreferences:prefs});
  avatar.setAlwaysOnTop(true,'screen-saver');
  secureWindow(avatar,url=>url===avatarPage);
  avatar.on('closed',()=>{if(!quitting) app.quit();});
  avatar.webContents.on('did-finish-load',()=>avatar.webContents.send('avatar:status',status));
  await avatar.loadFile(path.join(__dirname,'avatar.html'));
  avatar.show();
  avatar.focus();
  avatar.setAlwaysOnTop(true,'screen-saver');
  await chat.loadURL(BASE_URL).catch(()=>{chat.loadFile(path.join(__dirname,'offline.html'));});
}

ipcMain.on('assistant:status',(event,value)=>{
  if(event.sender!==chat?.webContents || !trustedChat(event.senderFrame?.url)) return;
  const next=sanitizeStatus(value); if(!next) return;
  status=next; if(avatar && !avatar.isDestroyed()) avatar.webContents.send('avatar:status',status);
});
ipcMain.on('avatar:action',(event,action)=>{
  if(event.sender!==avatar?.webContents || event.senderFrame?.url!==avatarPage) return;
  if(action==='open-chat') showChat();
  if(action==='toggle-mic') {if(trustedChat(chat?.webContents.getURL())) chat.webContents.send('assistant:command','toggle-mic'); else showChat();}
  if(action==='minimize') avatar.minimize();
  if(action==='quit') app.quit();
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
app.on('before-quit',()=>{quitting=true;});
app.on('window-all-closed',()=>app.quit());

