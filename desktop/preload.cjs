const {contextBridge, ipcRenderer} = require('electron');
contextBridge.exposeInMainWorld('desktopBridge', {
  publishStatus: status => ipcRenderer.send('assistant:status', status),
  onCommand: callback => {
    const handler = (_, command) => callback(command);
    ipcRenderer.on('assistant:command', handler);
    return () => ipcRenderer.removeListener('assistant:command', handler);
  },
  onStatus: callback => {
    const handler = (_, status) => callback(status);
    ipcRenderer.on('avatar:status', handler);
    return () => ipcRenderer.removeListener('avatar:status', handler);
  },
  avatarAction: action => ipcRenderer.send('avatar:action', action),
});
