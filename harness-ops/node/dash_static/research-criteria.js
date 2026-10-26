/* Author-defined criteria over a frozen comparison. Arithmetic belongs to the node. */
(() => {
  'use strict';
  const $ = id => document.getElementById(id), copy = value => JSON.parse(JSON.stringify(value));
  const SCOPES = {module:'单模块',game_loop:'游戏主循环',display:'显示',gpu_submit_proxy:'GPU 提交代理',power:'供电',image:'画质'};
  const ROLES = {objective:'目标',constraint:'约束',observe:'仅观察'}, PREFER = {minimize:'越小越好',maximize:'越大越好'};
  const RULES = {'':'无规则（只显示原值）',absolute_improvement_min:'改善量至少',relative_improvement_percent_min:'改善百分比至少',candidate_min:'候选值至少',candidate_max:'候选值至多',degradation_max:'最多允许退化'};
  const STATUS = {satisfied:'满足所设规则（点值）',not_satisfied:'不满足所设规则',unknown:'无法判断',observed:'仅观察',undetermined:'尚不能确定',not_configured:'未配置必要规则'};
  const REASONS = {value_unavailable:'登记数值缺失',axis_unit_mismatch:'与轴声明单位不同',axis_scope_mismatch:'与轴声明范围不同',unit_unavailable:'单位未记录',unit_mismatch:'比较单位不同',scope_unavailable:'范围未知',scope_mismatch:'比较范围不同',baseline_nonpositive:'基线不大于零，百分比未定义',not_selected:'未选择指标',result_unavailable:'结果不可读取',invalid_index:'指标索引越界',arithmetic_overflow:'算术溢出'};
  let editor = null, detail = null, locked = true;
  const supported = node => node?.capabilities?.includes('research.criteria-v1') === true;
  const el = (tag, value, className) => { const item = document.createElement(tag); if (value != null) item.textContent = String(value); if (className) item.className = className; return item; };
  const note = value => el('p', value, 'note');
  const display = value => value == null || value === '' ? '未提供' : typeof value === 'object' ? JSON.stringify(value) : String(value);
  const number = value => Number.isFinite(value) ? String(value) : '未知';
  function status(element, value, error=false) { element.textContent = value; element.classList.toggle('task-error', error); }
  function button(title,id) { const item=el('button',title,'ghost');item.type='button';if(id)item.id=id;return item; }
  function field(title,input) { const box=el('label');box.append(el('span',title),input);return box; }
  function select(options,value) { const item=el('select');for(const [key,title] of Object.entries(options))item.append(new Option(title,key));item.value=value;return item; }
  function check(title,checked=false) { const input=el('input');input.type='checkbox';input.checked=checked;const box=el('label',null,'criteria-check');box.append(input,el('span',title));return {input,box}; }
  function facts(values) { const box=el('dl',null,'facts');for(const [key,value] of values)box.append(el('dt',key),el('dd',display(value)));return box; }
  function jsonView(title,value) { const box=el('details',null,'research-json');box.append(el('summary',title),el('pre',JSON.stringify(value,null,2),'command-box'));return box; }
  function compareLink(spec,target,title='打开固定比较与原件') { const a=el('a',title);a.href=HGTarget.url('/dash/results?'+new URLSearchParams({compare:JSON.stringify(spec)}),target);return a; }
  function parseComparison(raw,target) {
    if(new TextEncoder().encode(raw).length>16384)throw new Error('比较 spec 超过 16 KiB。');
    const spec=JSON.parse(raw);
    if(!spec||spec.schema!=='phonefarm/result-compare@1'||spec.node_id!==target?.node_id)throw new Error('比较必须属于当前明确节点；不会按同名结果改投。');
    if(!Array.isArray(spec.columns)||spec.columns.length<2||spec.columns.length>4||!Array.isArray(spec.rows)||spec.rows.length<1||spec.rows.length>16)throw new Error('比较需 2–4 列与 1–16 行，请从结果页导出选择。');
    if(spec.columns.some(c=>!c||typeof c.id!=='string'||typeof c.result_id!=='string'||typeof c.label!=='string')||new Set(spec.columns.map(c=>c.id)).size!==spec.columns.length)throw new Error('比较列身份无效或重复。');
    if(spec.rows.some(r=>!r||typeof r.id!=='string'||typeof r.label!=='string'||!Array.isArray(r.metric_indexes)||r.metric_indexes.length!==spec.columns.length||r.metric_indexes.some(i=>i!==null&&(!Number.isInteger(i)||i<0||i>255)))||new Set(spec.rows.map(r=>r.id)).size!==spec.rows.length)throw new Error('比较行必须保留每列的明确指标索引或空项。');
    return spec;
  }
  function mountEditor(container,context) {
    editor=null;container.replaceChildren();if(context.kind!=='research')return;
    const old=context.data.criteria,exists=Object.prototype.hasOwnProperty.call(context.data,'criteria');
    const state={container,context,mode:exists?(old===null?'none':'configured'):'absent',comparison:old?.comparison?copy(old.comparison):null,baseline:old?.baseline_role==='original_game',axes:(old?.axes||[]).map(axis=>({...copy(axis),rule_kind:axis.rule?.kind||'',threshold:axis.rule?String(axis.rule.threshold):'',pareto:(old.pareto_axes||[]).includes(axis.id)})),nextAxis:1,loading:false};editor=state;
    const panel=el('section',null,'criteria-editor'),mode=select({absent:'尚未声明数值判据（保留字段缺席）',none:'此版本明确不设数值判据',configured:'配置数值判据'},state.mode);mode.id='criteria-mode';if(exists)mode.querySelector('option[value="absent"]').disabled=true;
    const message=note(''),fields=el('div',null,'criteria-config'),importBox=el('details',null,'research-json'),input=el('textarea'),file=el('input'),apply=button('使用此比较并重新配置轴','criteria-import-apply'),summary=el('div'),baseline=check('我声明首列是原游戏基线；这不表示平台验证了原生渲染路径。',state.baseline),axes=el('div',null,'criteria-axes'),add=button('增加判据轴','criteria-add-axis');
    input.id='criteria-comparison-input';input.rows=5;input.spellcheck=false;input.setAttribute('aria-label','比较选择 JSON');input.placeholder='粘贴结果页导出的 phonefarm/result-compare@1';file.type='file';file.accept='.json,application/json';file.id='criteria-comparison-file';baseline.input.id='criteria-baseline-role';summary.id='criteria-comparison-summary';axes.id='criteria-axes';
    importBox.append(el('summary','导入比较选择'),note('选择现有比较 spec；本页不重新匹配指标。应用新 spec 会清空当前轴，需重新确认基线与判据。'),field('小 JSON 文件（最多 16 KiB）',file),field('或粘贴比较 JSON',input),apply);
    fields.append(importBox,summary,baseline.box,el('h3','目标、容忍值与观察轴'),note('阈值不默认填零；模块 ms 可作为目标，画质可容忍下降，功耗可仅观察。至少一项目标；Pareto 轴由你明确勾选。'),axes,add);
    panel.append(el('h3','版本化数值判据'),field('本版本的判据设置',mode),message,fields);container.append(panel);
    state.summary=summary;state.axesElement=axes;state.message=message;
    function use(spec) { state.comparison=copy(spec);state.axes=[];state.baseline=false;baseline.input.checked=false;state.mode='configured';mode.value=state.mode;input.value='';file.value='';renderSummary(state);renderAxes(state);state.refresh();status(message,'比较已带入草稿，尚未保存；请确认首列角色并配置轴。'); }
    state.use=use;
    mode.addEventListener('change',()=>{state.mode=mode.value;state.refresh();});baseline.input.addEventListener('change',()=>{state.baseline=baseline.input.checked;});
    apply.addEventListener('click',()=>{if(locked)return;try{use(parseComparison(input.value,context.target));}catch(error){status(message,error.message,true);}});
    file.addEventListener('change',async()=>{
      const chosen=file.files[0];if(!chosen||locked)return;state.loading=true;state.refresh();
      try{if(chosen.size>16384)throw new Error('文件超过 16 KiB；请选择比较 spec，不是原始实验素材。');const raw=await chosen.text();if(editor!==state)return;parseComparison(raw,context.target);input.value=raw;status(message,'文件已在本页读取；请点击“使用此比较并重新配置轴”确认。');}
      catch(error){if(editor===state)status(message,error.message,true);}finally{state.loading=false;if(editor===state)state.refresh();}
    });
    add.addEventListener('click',()=>{if(locked||!state.comparison||state.axes.length>=16)return;while(state.axes.some(axis=>axis.id===`axis-${state.nextAxis}`))state.nextAxis++;state.axes.push({id:`axis-${state.nextAxis++}`,row_id:'',label:'',unit:'',scope:'',role:'objective',prefer:'',required:false,rule_kind:'',threshold:'',pareto:false});renderAxes(state);state.refresh();});
    state.refresh=()=>{
      const unavailable=!supported(context.node),disabled=locked||unavailable||!!HGTarget.problem(context.target);mode.disabled=disabled;fields.hidden=state.mode!=='configured';
      for(const control of fields.querySelectorAll('input,textarea,select,button'))control.disabled=disabled||state.loading;
      add.disabled=disabled||state.loading||!state.comparison||state.axes.length>=16;
      state.axisRefresh?.forEach(fn=>fn(disabled));
      if(unavailable)status(message,'节点未提供 research.criteria-v1；旧的无判据记录仍可原样编辑，不能新增或保存数值判据。',true);
      else if(HGTarget.problem(context.target))status(message,HGTarget.problem(context.target),true);
    };
    state.snapshot=()=>({mode:state.mode,comparison:copy(state.comparison),baseline_role_confirmed:state.baseline,axes:copy(state.axes),unapplied_comparison_json:input.value});
    renderSummary(state);renderAxes(state);state.refresh();
  }
  function renderSummary(state) {
    const box=state.summary;box.replaceChildren();const spec=state.comparison;
    if(!spec){box.append(note('尚未导入比较选择。可在结果页选择精确指标，再导出或带入研究草稿。'));return;}
    const list=el('ul',null,'research-evidence');spec.columns.forEach((column,index)=>list.append(el('li',`${index===0?'首列 / 待确认基线':`候选 ${index}`}：${column.label} · ${column.result_id}`)));
    box.append(note(`固定节点 ${spec.node_id} · ${spec.rows.length} 个比较行；允许预声明尚未登记的候选 ID。`),list,compareLink(spec,state.context.target,'在结果页查看此比较选择'));
    const rows=el('ul',null,'criteria-summary-rows');for(const row of spec.rows)rows.append(el('li',`${row.label}（${row.id}）：${row.metric_indexes.map(i=>i==null?'未选':`#${i}`).join(' / ')}`));box.append(rows);
    if(spec.context)box.append(facts([['作者声明范围',spec.context.scope],['假设',(spec.context.assumptions||[]).join('；')],['有意干预',(spec.context.interventions||[]).join('；')]]));
  }
  function renderAxes(state) {
    state.axesElement.replaceChildren();state.axisRefresh=[];
    for(const axis of state.axes){
      const box=el('section',null,'criteria-axis'),heading=el('div',null,'section-heading'),remove=button('移除此轴'),grid=el('div',null,'criteria-axis-grid');
      heading.append(el('h4',axis.id),remove);remove.addEventListener('click',()=>{state.axes=state.axes.filter(item=>item!==axis);renderAxes(state);state.refresh();});box.append(heading);
      const rows={'':'明确选择一个比较行'};for(const row of state.comparison?.rows||[])rows[row.id]=`${row.label} · ${row.id}`;
      const controls={row_id:select(rows,axis.row_id),label:el('input'),unit:el('input'),scope:select({'':'选择已知范围',...SCOPES},axis.scope),role:select(ROLES,axis.role),prefer:select({'':'选择方向',...PREFER},axis.prefer),rule_kind:select(RULES,axis.rule_kind),threshold:el('input')};
      controls.label.value=axis.label;controls.label.maxLength=128;controls.unit.value=axis.unit;controls.unit.maxLength=2048;controls.threshold.type='number';controls.threshold.step='any';controls.threshold.value=axis.threshold;
      const titles={row_id:'绑定比较行',label:'轴标题',unit:'要求的单位',scope:'要求的范围',role:'用途',prefer:'偏好方向',rule_kind:'判据规则',threshold:'阈值（相对改善为 %，其余为轴单位）'};
      for(const [key,input] of Object.entries(controls)){input.dataset.criteriaField=key;input.autocomplete='off';input.addEventListener(input.tagName==='SELECT'?'change':'input',()=>{axis[key]=input.value;if(key==='role'&&axis.role==='observe'){axis.required=false;axis.rule_kind='';axis.threshold='';controls.rule_kind.value='';controls.threshold.value='';required.input.checked=false;}if(key==='rule_kind'&&!axis.rule_kind){axis.required=false;required.input.checked=false;}state.refresh();});grid.append(field(titles[key],input));}
      const required=check('必须满足此规则（参与必要条件汇总）',axis.required),pareto=check('此轴纳入点值 Pareto（不自动代替约束判据）',axis.pareto);required.input.dataset.criteriaField='required';pareto.input.dataset.criteriaField='pareto';required.input.addEventListener('change',()=>{axis.required=required.input.checked;});pareto.input.addEventListener('change',()=>{axis.pareto=pareto.input.checked;});
      box.append(grid,required.box,pareto.box,note('改善按偏好方向计算；允许退化可为画质设容忍值。无规则仅观察原值，不能勾选为必须满足。'));state.axesElement.append(box);
      state.axisRefresh.push(disabled=>{const off=disabled||state.mode!=='configured';for(const control of Object.values(controls))control.disabled=off;required.input.disabled=off||axis.role==='observe'||!axis.rule_kind;pareto.input.disabled=off;remove.disabled=off;controls.rule_kind.disabled=off||axis.role==='observe';controls.threshold.disabled=off||axis.role==='observe'||!axis.rule_kind;});
    }
  }
  function collect(data) {
    if(!editor)return;const s=editor;
    if(s.mode==='absent'){delete data.criteria;return;}
    if(!supported(s.context.node))throw new Error('节点未提供 research.criteria-v1，不能保存判据字段。');
    if(s.mode==='none'){data.criteria=null;return;}
    if(!s.comparison)throw new Error('请先导入固定比较 spec。');parseComparison(JSON.stringify(s.comparison),s.context.target);
    if(!s.baseline)throw new Error('请明确声明比较首列为原游戏基线；平台不会代为推断。');
    if(!s.axes.length||s.axes.length>16)throw new Error('请配置 1–16 个判据轴。');
    const rows=new Set(),axes=s.axes.map(axis=>{
      if(!s.comparison.rows.some(row=>row.id===axis.row_id)||rows.has(axis.row_id))throw new Error('每轴需绑定一个不同的比较行；不会自动匹配指标。');rows.add(axis.row_id);
      if(!axis.label.trim()||!axis.unit.trim()||!SCOPES[axis.scope]||!ROLES[axis.role]||!PREFER[axis.prefer])throw new Error('请为每轴填写标题、单位、已知范围、用途和偏好方向。');
      let rule=null;if(axis.rule_kind){const threshold=axis.threshold.trim()===''?NaN:Number(axis.threshold);if(!Number.isFinite(threshold)||(['absolute_improvement_min','relative_improvement_percent_min','degradation_max'].includes(axis.rule_kind)&&threshold<0))throw new Error('请明确填写有限阈值；改善量和允许下降量不能为负。');rule={kind:axis.rule_kind,threshold};}
      if(axis.role==='observe'&&(rule||axis.required)||axis.required&&!rule)throw new Error('仅观察轴不能有规则或 required；必要条件必须指定规则。');
      return {id:axis.id,row_id:axis.row_id,label:axis.label.trim(),unit:axis.unit.trim(),scope:axis.scope,role:axis.role,prefer:axis.prefer,required:axis.required,rule};
    });
    if(!axes.some(axis=>axis.role==='objective'))throw new Error('至少需要一个目标轴；平台不自动指定优化方向。');
    data.criteria={schema:'phonefarm/research-criteria@1',comparison:copy(s.comparison),baseline_role:'original_game',axes,pareto_axes:s.axes.filter(axis=>axis.pareto).map(axis=>axis.id)};
  }
  function ruleText(axis,rule=axis.rule) { return rule?`${RULES[rule.kind]||rule.kind} ${number(rule.threshold)} ${rule.kind==='relative_improvement_percent_min'?'%':axis.unit}`:'无规则，只观察原值'; }
  function bindingView(bindings) {
    const box=el('details',null,'research-json');box.append(el('summary','保存判据时的结果身份绑定'));
    for(const binding of bindings||[])box.append(facts([['列',binding.column_id],['节点 / 结果',`${display(binding.node_id)} / ${display(binding.result_id)}`],['当时状态',binding.status==='declared_pending'?'保存时未绑定登记（预声明）':binding.status==='registration_bound'?'已绑定当时登记身份':binding.status],['当时序号',binding.result_sequence],['当时登记时间',Number.isFinite(binding.result_registered_at)?new Date(binding.result_registered_at*1000).toLocaleString():null]]));
    if(!bindings?.length)box.append(note('未提供身份绑定；不补造当时已见的结果。'));return box;
  }
  function clearDetail() { if(detail){detail.epoch++;detail.controller?.abort();}detail=null;$('criteria-detail').replaceChildren(); }
  function renderDetail(container,record,context) {
    clearDetail();if(record.kind!=='research')return;
    const panel=el('section',null,'research-section criteria-detail'),criteria=record.data.criteria;
    panel.append(el('h3','版本化判据与点值判断'));
    if(!criteria){panel.append(note(Object.prototype.hasOwnProperty.call(record.data,'criteria')?'此版本明确没有数值判据。':'此版本未声明数值判据；不会从目标文字补阈值。'));container.append(panel);return;}
    const s={record,context,epoch:0,controller:null,busy:false},message=note('尚未读取判断；将固定此研究版本，不自动使用最新判据。'),read=button(`判断 v${record.revision} 的登记点值`,'criteria-assess'),output=el('div',null,'criteria-assessment');detail=s;s.message=message;s.read=read;
    panel.append(note(`研究 ${record.id} · v${record.revision}（当前 v${record.current_revision}）。首列“${criteria.comparison.columns[0].label}”由作者声明为原游戏基线，未验证原生渲染路径。`),compareLink(criteria.comparison,context.target),bindingView(record.derived?.criteria_bindings));
    const rules=el('ul',null,'research-evidence');for(const axis of criteria.axes)rules.append(el('li',`${axis.label} · ${ROLES[axis.role]||axis.role} · ${PREFER[axis.prefer]||axis.prefer} · ${axis.unit} / ${SCOPES[axis.scope]||axis.scope} · ${ruleText(axis)} · ${axis.required?'必要条件':'可选'}${criteria.pareto_axes.includes(axis.id)?' · 纳入 Pareto':''}`));panel.append(rules,read,message,output);container.append(panel);
    s.refresh=()=>{read.disabled=s.busy||!supported(context.node)||!!HGTarget.problem(context.target);read.setAttribute('aria-busy',String(s.busy));read.textContent=s.busy?'正在读取固定版本…':`判断 v${record.revision} 的登记点值`;if(!supported(context.node))status(message,'节点未提供 research.criteria-v1；保留已有判据，判断接口不可用。',true);else if(HGTarget.problem(context.target))status(message,HGTarget.problem(context.target),true);};
    read.addEventListener('click',async()=>{
      if(s.busy||!supported(context.node)||HGTarget.problem(context.target))return;const epoch=++s.epoch,controller=new AbortController(),timer=setTimeout(()=>controller.abort(),20000);s.controller=controller;s.busy=true;s.refresh();output.hidden=true;status(message,`正在只读判断 ${record.id} v${record.revision}…`);
      try{const value=await context.readAssessment(record.id,record.revision,controller.signal);if(detail!==s||epoch!==s.epoch)return;if(HGTarget.problem(context.target))throw new Error(HGTarget.problem(context.target));if(value.schema_version!==1||value.node_id!==context.target.node_id||value.research_id!==record.id||value.revision!==record.revision||value.basis!=='registered_point_values'||value.comparability!=='not_established')throw new Error('判断的节点、研究或 revision 不匹配；原判据保留。');renderAssessment(output,value,context.target);output.hidden=false;status(message,`已读取 v${value.revision} 的判断，当前研究 v${value.current_revision}。草稿与方向状态未改。`);}
      catch(error){if(detail===s&&epoch===s.epoch)status(message,`判断读取失败：${error.name==='AbortError'?'超时或取消':error.message}。保留原版本，可显式重试。`,true);}
      finally{clearTimeout(timer);if(detail===s&&epoch===s.epoch){s.busy=false;s.refresh();}}
    });s.refresh();
  }
  function renderAssessment(output,value,target) {
    output.replaceChildren();const criteria=value.criteria,columns=new Map(value.comparison.columns.map(column=>[column.id,column])),axes=new Map(criteria.axes.map(axis=>[axis.id,axis])),axisNames=ids=>(ids||[]).map(id=>axes.get(id)?.label||id).join('、')||'无';
    output.append(note('以下仅是作者所选单位、范围、假设下的登记点值判断；不是统计显著、因果、显示 FPS 或自动采纳结论。原结果、任务与设备恢复状态分别保留。'),compareLink(criteria.comparison,target,'打开同一比较矩阵、描述差异与原件'));
    if(criteria.comparison.context)output.append(facts([['声明范围',criteria.comparison.context.scope],['假设',(criteria.comparison.context.assumptions||[]).join('；')],['有意干预',(criteria.comparison.context.interventions||[]).join('；')]]));
    const pareto=value.pareto,p=el('section',null,'criteria-pareto');p.append(el('h4','条件性点值 Pareto'),note(pareto.status==='not_requested'?'未请求 Pareto，没有自动选择轴。':`仅在所选轴都有可用值的列中比较；不删轴、不打总分，不表示全局最优。轴：${axisNames(pareto.axis_ids)}。`));
    if(pareto.excluded_axis_ids?.length)p.append(note(`未参与轴：${axisNames(pareto.excluded_axis_ids)}；请核对完整回包。`));if(pareto.undetermined_column_ids?.length)p.append(note('无法确定的列：'+pareto.undetermined_column_ids.map(id=>columns.get(id)?.label||id).join('、')));
    for(const item of pareto.columns||[]){const box=el('div',null,'criteria-pareto-column');box.append(el('strong',`${columns.get(item.column_id)?.label||item.column_id}：${{non_dominated:'完整值子集中未被支配',dominated:'在完整值子集中被支配',undetermined:'缺项，不能确定',not_requested:'未请求'}[item.status]||item.status}`));if(item.dominated_by?.length)box.append(note('点值支配来源：'+item.dominated_by.map(id=>columns.get(id)?.label||id).join('、')));for(const itemReason of item.reasons||[])box.append(note(`${axes.get(itemReason.axis_id)?.label||itemReason.axis_id}：${REASONS[itemReason.reason]||itemReason.reason}`));p.append(box);}output.append(p);
    for(const candidate of value.candidates){const column=columns.get(candidate.column_id),card=el('section',null,'criteria-candidate'),requirement=candidate.requirements;
      card.append(el('h4',`${candidate.column_id===value.comparison.baseline_column_id?'基线':'候选'} · ${column?.label||candidate.column_id}`),note(`${column?.node_id||value.node_id} / ${column?.result_id||'结果未知'}`),facts([['必要条件汇总',STATUS[requirement.status]||requirement.status],['必要轴',axisNames(requirement.required_axes)],['未满足轴',axisNames(requirement.failed_axes)],['未知轴',axisNames(requirement.unknown_axes)]]));
      if(column?.status==='unavailable')card.append(note('结果不可查：'+(column.error?.message||'未提供原因')));
      for(const item of candidate.axes){const axis=axes.get(item.axis_id)||{},row=el('div',null,'criteria-axis-result');row.append(el('strong',`${axis.label||item.axis_id} · ${STATUS[item.status]||item.status}`),facts([['原登记值',`${number(item.value)} ${item.unit||'（单位未知）'}`],['所设规则',ruleText(axis,item.rule)],['规则使用的数',`${number(item.observed)} ${item.rule?.kind==='relative_improvement_percent_min'?'%':item.unit||''}`],['偏好 / 范围',`${PREFER[axis.prefer]||display(axis.prefer)} / ${SCOPES[axis.scope]||display(axis.scope)}`],['原因',REASONS[item.reason]||item.reason||'无额外原因']]));card.append(row);}
      const result=column?.result;if(result){const link=el('a','查看此列原结果与来源');link.href=HGTarget.url(`/dash/results#result=${encodeURIComponent(column.result_id)}`,target);card.append(link,note(`本次读到登记序号 ${display(result.sequence)}；原报告结论：${display(result.manifest?.outcome)}。`));for(const source of result.current?.sources||[])card.append(note(`当前来源 ${source.name}：${{available:'可读',unavailable:'不可读',changed:'已变化',external:'外部声明'}[source.status]||source.status}${source.reason?' · '+source.reason:''}`));if(result.current?.job)card.append(note(`原任务：${display(result.current.job.record?.phase||result.current.job.status)} · 恢复：${display(result.current.job.record?.recovery?.status)}；与数值判据独立。`));}output.append(card);
    }
    output.append(renderSuggestions(value,columns,axes),bindingView(value.criteria_bindings),jsonView('完整只读判断回包',value));
  }
  function renderSuggestions(value,columns,axes) {
    const panel=el('section',null,'research-section');panel.append(el('h4','下一实验建议'),note('以下选项基于此研究版本的登记证据；顺序不代表优先级，由外部 Agent 判断。不会自动提交实验、解除依赖或修改判据。'));
    if(!Array.isArray(value.suggestions)){panel.append(note('此回包未提供下一实验建议；原判断与未知项仍保留。'));return panel;}
    if(!value.suggestions.length){panel.append(note('本次未生成建议，不表示证据完整或研究已完成。'));return panel;}
    for(const suggestion of value.suggestions){
      const card=el('section',null,'criteria-candidate'),message=note(''),copyButton=button('复制此建议'),payload={node_id:value.node_id,research_id:value.research_id,revision:value.revision,suggestion:copy(suggestion)};
      card.append(el('h4',suggestion.title),facts([['适用原因',suggestion.reason],['可做的下一步',suggestion.next_experiment]]));
      if(suggestion.affected?.length){const affected=el('ul',null,'research-evidence');for(const item of suggestion.affected){const column=columns.get(item.column_id),axis=axes.get(item.axis_id);affected.append(el('li',`${column?.label||item.column_id} / ${item.axis_id==null?'整列结果':axis?.label||item.axis_id}：${REASONS[item.reason]||item.reason}`));}card.append(affected);}
      copyButton.addEventListener('click',async()=>{try{await navigator.clipboard.writeText(JSON.stringify(payload,null,2));status(message,'已复制原建议及节点、研究版本；尚未创建方向或保存计划。');}catch(_){status(message,'剪贴板不可用，请展开下方原建议 JSON 手动复制。',true);}});
      card.append(copyButton,message,jsonView('原建议与固定研究版本',payload));panel.append(card);
    }
    return panel;
  }
  function setLocked(value){locked=value;editor?.refresh?.();}
  function importComparison(spec){if(!editor||locked)throw new Error('请先打开可编辑的研究草稿。');editor.use(parseComparison(JSON.stringify(spec),editor.context.target));}
  document.addEventListener('hgamey-target-change',()=>{editor?.refresh?.();if(detail&&HGTarget.problem(detail.context.target)){detail.epoch++;detail.controller?.abort();detail.busy=false;}detail?.refresh?.();});
  window.addEventListener('pagehide',()=>{clearDetail();editor=null;});
  window.HGResearchCriteria={mountEditor,setLocked,collect,snapshot:()=>editor?.snapshot?.(),renderDetail,clearDetail,parseComparison,importComparison,supported};
})();
