const test=require('node:test'),assert=require('node:assert/strict');
const fs=require('node:fs'),path=require('node:path');
const {BACKEND_FILES,BACKEND_DIRS}=require('../build.cjs');
const root=path.resolve(__dirname,'..','..');
test('package allowlist points at existing backend files and domain packages',()=>{
  for(const file of BACKEND_FILES)assert.ok(fs.statSync(path.join(root,file)).isFile(),file);
  assert.ok(BACKEND_DIRS.includes('team'));
  for(const dir of BACKEND_DIRS)assert.ok(fs.statSync(path.join(root,dir,'__init__.py')).isFile(),dir);
});
test('allowlist excludes local configuration and data',()=>{
  for(const forbidden of ['config.json','voice-settings.json','validacao.json'])assert.ok(!BACKEND_FILES.includes(forbidden));
});
