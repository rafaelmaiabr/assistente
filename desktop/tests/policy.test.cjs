const test=require('node:test'),assert=require('node:assert/strict');
const {trustedChat,sanitizeStatus,audioPermission}=require('../policy.cjs');
test('only the exact local chat can publish or capture',()=>{
  assert.equal(trustedChat('http://127.0.0.1:8765/'),true);
  for(const url of ['https://evil.example','http://127.0.0.1:8765/evil','http://127.0.0.1:8765.evil/','file:///tmp/page']) assert.equal(trustedChat(url),false);
  assert.equal(audioPermission('http://127.0.0.1:8765/',['audio']),true);
  assert.equal(audioPermission('http://127.0.0.1:8765/',['video']),false);
  assert.equal(audioPermission('https://evil.example',['audio']),false);
});
test('state bridge rejects unknown payloads and strips arbitrary content',()=>{
  assert.equal(sanitizeStatus({state:'execute-shell'}),null);
  assert.deepEqual(sanitizeStatus({state:'ready',micEnabled:true,level:5,secret:'private'}),{state:'ready',micEnabled:true,level:1});
  assert.equal(sanitizeStatus({state:'ready',level:NaN}).level,0);
});
