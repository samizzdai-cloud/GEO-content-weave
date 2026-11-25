// Deterministic frontend state tests; no browser or provider is invoked.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync('geo_workbench/static/workbench.js', 'utf8').split('\nlist=async function')[0];
const nodes = new Map();
for (const id of ['intakeReply','revisedQuery','productEditor','feedback','planEditor','contentFeedback']) {
  nodes.set(id, {id, value:'', dataset:{}});
}
const saved = new Map();
const sandbox = {
  current:'task-a', info:{plan_rev:1, product:{product:{name:'A'}}, user_query:'A query'}, selectedAttempt:'attempt-a',
  load(){}, openRun(){}, showArtifacts(){}, text(){},
  $:id=>nodes.get(id),
  document:{addEventListener(){}},
  localStorage:{getItem:key=>saved.get(key)||null,setItem:(key,value)=>saved.set(key,value),removeItem:key=>saved.delete(key)},
  api:async()=>({accepted:true}),
};
vm.createContext(sandbox);
vm.runInContext(source, sandbox);
vm.runInContext('restoreDrafts()', sandbox);
const review = nodes.get('contentFeedback');
review.value = 'A review';review.dataset.edited='1';
vm.runInContext('saveDrafts()', sandbox);
assert.equal(JSON.parse(saved.get('geo-draft:task-a:contentFeedback:attempt-a')).value, 'A review');
// A stale node retains its original owner even after global selection changes.
sandbox.current='task-b';sandbox.selectedAttempt='attempt-b';
vm.runInContext('saveDrafts()', sandbox);
assert.equal(saved.has('geo-draft:task-b:contentFeedback:attempt-b'), false);
const plan = nodes.get('planEditor');
plan.value='old plan draft';plan.dataset.edited='1';
sandbox.info.plan_rev=2;
vm.runInContext('saveDrafts()', sandbox);
assert.equal(JSON.parse(saved.get('geo-draft:task-a:planEditor:1')).value,'old plan draft');
assert.equal(saved.has('geo-draft:task-b:planEditor:2'),false);
// A late response clears only the captured owner/version, never the new task's draft.
(async()=>{
  sandbox.current='task-a';sandbox.selectedAttempt='attempt-a';
  review.dataset.draftKey='geo-draft:task-a:contentFeedback:attempt-a';
  const response = vm.runInContext("api('/api/runs/task-a/content-review',{method:'POST'})", sandbox);
  sandbox.current='task-b';sandbox.selectedAttempt='attempt-b';
  review.dataset.draftKey='geo-draft:task-b:contentFeedback:attempt-b';review.value='B review';
  saved.set(review.dataset.draftKey,JSON.stringify({value:'B review'}));
  await response;
  assert.equal(saved.has('geo-draft:task-a:contentFeedback:attempt-a'),false);
  assert.equal(JSON.parse(saved.get('geo-draft:task-b:contentFeedback:attempt-b')).value,'B review');
  assert.equal(review.value,'B review');
  assert.equal(vm.runInContext('pendingWrites', sandbox),0);
  console.log('PASS: draft task isolation, revision isolation, late response ownership');
})().catch(error=>{console.error(error);process.exitCode=1});
