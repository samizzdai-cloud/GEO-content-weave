/* Per-candidate editors and drafts; one page-level selected image and release. */
async function renderImageWorkspace(box,rid,role,attempt,frame){
 const card=el('section','card');card.setAttribute('aria-label','图片与图文确认');box.append(card);let stamp=null;
 const refresh=async()=>{
  if(current!==rid||businessView!==role||!card.isConnected)return;
  const d=await api(`/api/runs/${rid}/page-images/${role}`);
  if(current!==rid||businessView!==role||!card.isConnected)return;
  if(d.binding.attempt_id!==attempt){card.replaceChildren(el('p','note','历史正文仅供查看；请切换最新版本配图。'));return}
  const next=JSON.stringify(d);if(stamp===next)return;stamp=next;
  card.replaceChildren(el('h3','','图片与图文确认'));
  const error=el('p','error');error.setAttribute('role','alert');card.append(error);
  const post=async(suffix,body)=>api(`/api/runs/${rid}/page-images/${role}${suffix}`,{method:'POST',body});
  const request=extra=>({attempt_id:attempt,revision:d.binding.text_revision,idempotency_key:crypto.randomUUID(),...extra});
  const selected=d.session?.selected_asset;
  if(selected){const preview=`/api/runs/${rid}/page-images/${role}/image-preview?asset_id=${encodeURIComponent(selected)}`;if(!frame.src.endsWith(preview))frame.src=preview}
  card.append(el('p','muted',d.release?'当前图文版本已确认。':role==='blog'?'比较“使用者入镜”和“仅商品与场景”，选择一张用于 Blog。使用者依据商品判断，可为人或动物等；两张可分别修改。':'检查图片并确认当前图文版本。'));
  const candidates=d.candidates?.length?d.candidates:[{candidate_key:'legacy',session:d.session,assets:d.assets,jobs:d.jobs}];
  const title=k=>({with_user:'A · 使用者入镜',without_user:'B · 仅商品与场景',legacy:role==='blog'?'此前单图与历史':'商品详情图'})[k];
  const draftKey=k=>`geo-image-draft:${rid}:${role}:${attempt}:${k}`;
  const readDraft=(key,last)=>localStorage.getItem(draftKey(key))??(key==='legacy'?localStorage.getItem(`geo-image-draft:${rid}:${role}:${attempt}`):null)??(last?.status==='failed'?last.message:d.default_brief||'');
  if(role==='blog'&&candidates.filter(c=>c.candidate_key!=='legacy').every(c=>!c.session)){
   const both=el('button','','生成两版候选图');both.onclick=async()=>{both.disabled=true;try{
    for(const key of ['with_user','without_user'])await post('',request({candidate_key:key,message:readDraft(key),transparent:false}));
    await refresh();
   }catch(e){error.textContent=e.message;stamp=null;await refresh()}};card.append(both);
  }
  if(role==='blog'&&candidates.find(c=>c.candidate_key==='legacy')?.assets.some(a=>a.id===selected))card.append(el('p','note','当前预览保留此前选图；确认新候选后会替换。'));
  const grid=el('div');grid.style.cssText='display:grid;grid-template-columns:repeat(auto-fit,minmax(min(100%,320px),1fr));gap:20px;align-items:start';card.append(grid);
  for(const c of candidates){
   const key=c.candidate_key,last=c.jobs.at(-1),busy=c.jobs.some(j=>['queued','running'].includes(j.status));
   const active=c.assets.find(a=>a.id===c.session?.selected_asset)||c.assets.at(-1);
   const pane=el('section','card');pane.setAttribute('aria-label',title(key));if(role==='blog'&&key==='legacy'){const old=el('details');old.append(el('summary','','此前单图与历史'),pane);card.append(old)}else grid.append(pane);pane.append(el('h4','',title(key)));
   const localError=el('p','error');localError.setAttribute('role','alert');pane.append(localError);if(last?.error)localError.textContent=last.error;
   if(busy)pane.append(el('p','muted','此候选正在排队或生成，另一候选可单独操作。'));
   const draft=draftKey(key),label=el('label','',active?'修改这张图（只引用本卡当前版本）':'首次配图要求（可选）'),input=el('textarea');
   input.placeholder=active?'描述修改和保留内容；只传本图作参考，返回后由你检查。':'补充场景、主体或风格要求';
   input.value=active?(localStorage.getItem(draft)??(last?.status==='failed'&&last.mode==='user_edit'?last.message:'')):readDraft(key,last);input.disabled=busy;input.oninput=()=>{try{localStorage.setItem(draft,input.value)}catch{localError.textContent='意见无法保存，请复制后切换。'}};
   if(active){
    const img=el('img');img.src=`/api/runs/${rid}/image-assets/${active.id}.png`;img.alt=active.alt;img.style.cssText='width:100%;max-height:300px;object-fit:contain;background:#edf0f5';pane.append(img);
    for(const [text,url] of [['放大查看',img.src],['下载 PNG',img.src+'?download=true']]){const a=el('a','secondary',text);a.href=url;if(text==='放大查看'){a.target='_blank';a.rel='noopener'}else a.download=role+'-'+key+'.png';pane.append(a)}
    field(pane,'图片说明',active.caption_en);field(pane,'Alt Text',active.alt);
    const visual=active.visual_review;field(pane,'视觉检查',({not_run_user_edit:'按你的意见修改，未再自动视觉检查',pass:'自动自检通过，仍请检查',revise:'系统建议修改，请检查',unknown:'视觉检查未完成'})[visual.status]||visual.status);
    if(visual.status!=='pass')field(pane,'检查建议',visual.issues);
    if(!active.file_checks.pass)pane.append(el('p','error','文件、比例或透明通道检查未通过。'));
    const details=el('details');details.append(el('summary','','Prompt 与生成记录'),el('pre','',active.prompt));field(details,'你的原话',active.user_quote);field(details,'模型',active.generator.model);field(details,'自检备注',visual.notes);pane.append(details);
    const versions=el('details');versions.append(el('summary','',`本候选版本（${c.assets.length}）`));
    for(const a of c.assets){const b=el('button','secondary',a.id===active.id?'本卡当前版本':`查看版本 ${c.assets.indexOf(a)+1}`);b.disabled=busy||a.id===active.id;b.onclick=async()=>{b.disabled=true;try{await post('/select',{asset_id:a.id});await refresh()}catch(e){localError.textContent=e.message;b.disabled=false}};versions.append(b)}pane.append(versions);
    const use=el('button','secondary',selected===active.id?'当前 Blog / 页面预览用图':'预览此图');use.disabled=busy||selected===active.id;use.onclick=async()=>{try{await post('/select',{asset_id:active.id});await refresh()}catch(e){localError.textContent=e.message}};pane.append(use);
   }
   label.append(input);pane.append(label);
   let transparentChoice=c.session?c.session.config.background==='transparent':role==='product_page'&&localStorage.getItem(draft+':transparent')!=='false';
   if(role==='product_page'&&!active){const toggle=el('input');toggle.type='checkbox';toggle.checked=transparentChoice;toggle.disabled=!!c.session;toggle.onchange=()=>{transparentChoice=toggle.checked;localStorage.setItem(draft+':transparent',String(transparentChoice))};const bgLabel=el('label','','使用透明背景');bgLabel.append(toggle);pane.append(bgLabel)}
   const action=el('button','',active?'按我的意见修改此图':last?'核查后重试此候选':'生成此候选');action.disabled=busy||(!active&&c.session?.auto_calls>=3);
   action.onclick=async()=>{if(active&&!input.value.trim()){localError.textContent='请填写修改意见';return}action.disabled=true;const submitted=input.value;try{
    await post('',request({candidate_key:key,mode:active?'user_edit':'auto_initial',message:submitted,reference:active?.id,transparent:transparentChoice,retry:!active&&!!last}));
    if(localStorage.getItem(draft)===submitted)localStorage.removeItem(draft);await refresh();
   }catch(e){localError.textContent=e.message;action.disabled=false}};pane.append(action);
   if(active){const confirmed=d.release?.asset_id===active.id,confirm=el('button',confirmed?'secondary':'',confirmed?'此图文版本已确认':role==='blog'?'确认此图并用于 Blog':'确认当前图文版本');confirm.disabled=busy||confirmed||!active.file_checks.pass;
    confirm.onclick=async()=>{confirm.disabled=true;try{await post('/confirm',{asset_id:active.id,attempt_id:attempt,revision:d.binding.text_revision});await refresh()}catch(e){localError.textContent=e.message;confirm.disabled=false}};pane.append(confirm)}
  }
  if(d.release){const html=el('a','secondary',role==='blog'?'下载已确认 Blog HTML':'下载已确认详情页 HTML');html.href=`/api/runs/${rid}/page-images/${role}/delivery.html`;html.download=role+'.html';card.append(html)}
  const bundle=el('button','secondary','下载图文交付包（两页确认后）');bundle.onclick=async()=>{try{const r=await fetch(`/api/runs/${rid}/image-delivery.zip`);if(!r.ok)throw Error((await r.json()).detail||'交付包尚未准备好');const url=URL.createObjectURL(await r.blob()),a=el('a');a.href=url;a.download='image-delivery.zip';a.click();setTimeout(()=>URL.revokeObjectURL(url),1000)}catch(e){error.textContent=e.message}};card.append(bundle);
 };
 box.refreshImages=refresh;try{await refresh()}catch(e){card.replaceChildren(el('p','error','图片区域：'+e.message))}
}
