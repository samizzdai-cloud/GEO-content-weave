const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm');
class Element{
 constructor(tag,cls,text){this.tag=tag;this.className=cls||'';this.textContent=text||'';this.children=[];this.style={};this.isConnected=true;this.src=''}
 append(...items){this.children.push(...items)} replaceChildren(...items){this.children=items} setAttribute(k,v){this[k]=v}
}
const makeAsset=id=>({id,alt:'illustration',caption_en:'Illustration',prompt:'a picture',file_checks:{pass:true},visual_review:{status:'pass',issues:[]},generator:{model:'fixture'}});
const a=makeAsset('a'),b=makeAsset('b');
let state={binding:{attempt_id:'attempt',text_revision:1},session:{selected_asset:'a'},release:null,assets:[a],jobs:[],
 candidates:[{candidate_key:'with_user',session:{selected_asset:'a',config:{background:'opaque'}},assets:[a],jobs:[]},
             {candidate_key:'without_user',session:{selected_asset:'b',config:{background:'opaque'}},assets:[b],jobs:[]}]};
const saved=new Map(),posts=[];
const context={current:'run',businessView:'blog',crypto:{randomUUID:()=>String(posts.length)},
 el:(...args)=>new Element(...args),field:()=>{},localStorage:{getItem:k=>saved.get(k)??null,setItem:(k,v)=>saved.set(k,v),removeItem:k=>saved.delete(k)},
 api:async(path,opts)=>{if(!opts)return structuredClone(state);posts.push({path,body:opts.body});
  if(path.endsWith('/confirm')){state.release={asset_id:opts.body.asset_id};state.session.selected_asset=opts.body.asset_id}
  else if(path.endsWith('/select'))state.session.selected_asset=opts.body.asset_id;
  else {const target=state.candidates.find(c=>c.candidate_key===opts.body.candidate_key);if(target)target.jobs=[{mode:'user_edit',status:'queued'}]}return {accepted:true}},
 fetch:()=>{throw Error('unexpected download')},URL,Blob,setTimeout};
vm.createContext(context);vm.runInContext(fs.readFileSync('geo_workbench/static/images.js','utf8'),context);
const walk=x=>[x,...x.children.flatMap(c=>c instanceof Element?walk(c):[])];
const pane=(box,name)=>walk(box).find(n=>n['aria-label']===name);
const button=(box,text)=>walk(box).find(n=>n.tag==='button'&&n.textContent===text);
(async()=>{
 const box=new Element('div'),frame=new Element('iframe');
 await context.renderImageWorkspace(box,'run','blog','attempt',frame);
 let pa=pane(box,'A · 使用者入镜'),pb=pane(box,'B · 仅商品与场景');
 const ta=walk(pa).find(n=>n.tag==='textarea'),tb=walk(pb).find(n=>n.tag==='textarea');
 ta.value='Change A lighting';ta.oninput();tb.value='Keep B independent';tb.oninput();
 await button(pa,'按我的意见修改此图').onclick();
 assert.equal(posts[0].body.reference,'a');assert.equal(posts[0].body.candidate_key,'with_user');assert.equal(posts[0].body.message,'Change A lighting');
 pb=pane(box,'B · 仅商品与场景');assert.equal(walk(pb).find(n=>n.tag==='textarea').value,'Keep B independent');
 assert.equal(saved.get('geo-image-draft:run:blog:attempt:with_user'),undefined);
 await button(pb,'确认此图并用于 Blog').onclick();
 assert.equal(posts.at(-1).body.asset_id,'b');assert.match(frame.src,/asset_id=b$/);
 assert(walk(box).some(n=>n.tag==='a'&&n.textContent==='下载已确认 Blog HTML'));
 context.businessView='product_page';state={binding:{attempt_id:'attempt',text_revision:1},session:null,assets:[],jobs:[],release:null,candidates:[]};
 const pdp=new Element('div');await context.renderImageWorkspace(pdp,'run','product_page','attempt',new Element('iframe'));
 const toggle=walk(pdp).find(n=>n.type==='checkbox');assert.equal(toggle.checked,true);toggle.checked=false;toggle.onchange();
 await button(pdp,'生成此候选').onclick();assert.equal(posts.at(-1).body.transparent,false);
 console.log('PASS candidate drafts, single reference, selection, HTML action and PDP background option');
})().catch(e=>{console.error(e);process.exitCode=1});
