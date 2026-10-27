import { resourceResponse } from "./resource-response";
import {expect,test,type Page} from '@playwright/test';
async function setup(page:Page){
 const scope={id:'personal',name:'个人',kind:'personal',permissions:[]};
 const kb={id:'kb',name:'制度库',description:'办公资料',scope_id:'personal',scope,enabled:true,actions:{use:true,manage:true,download:true},created_at:'2026-09-14T00:00:00Z'};
 const calls:{binding?:unknown;upload:boolean;retry:boolean;deleted:boolean}={upload:false,retry:false,deleted:false};
 let status='error';
 await page.addInitScript(()=>{localStorage.setItem('workmate_token','test');localStorage.setItem('workmate_user',JSON.stringify({id:'u',email:'test@example.com',display_name:'测试',status:'active',must_change_password:false}));});
 await page.route('**/api/**',async route=>{
 const path=new URL(route.request().url()).pathname;const method=route.request().method();const json=(body:unknown)=>route.fulfill({contentType:'application/json',body:JSON.stringify(resourceResponse(route.request().url(), body))});
 if(path==='/api/auth/me/permissions')return json({permissions:[],personal_scope_id:'personal',is_root_admin:false});
 if(path==='/api/auth/me')return json({id:'u',email:'test@example.com',display_name:'测试',status:'active',must_change_password:false});
 if(path==='/api/scopes/accessible')return json([scope]);
 if(path==='/api/knowledge-bases')return json([kb]);
 if(path==='/api/knowledge-bases/kb')return json(kb);
 if(path==='/api/knowledge-bases/kb/documents'&&method==='POST'){calls.upload=true;return json({id:'doc'});}
 if(path==='/api/knowledge-bases/kb/documents')return json({items:calls.deleted?[]:[{id:'doc',filename:'制度.txt',status,error:status==='error'?'未提取到文本':null,chunk_count:status==='ready'?2:0}],total:calls.deleted?0:1});
 if(path.endsWith('/retry')){calls.retry=true;status='ready';return json({});}
 if(path==='/api/knowledge-bases/kb/documents/doc'&&method==='DELETE'){calls.deleted=true;return route.fulfill({status:204});}
 if(path.endsWith('/search'))return json({sources:[{source_id:'s',knowledge_base_id:'kb',document_id:'doc',filename:'制度.txt',chunk_index:0,metadata:{page:1},excerpt:'请假须提前申请。',score:1}]});
 if(path.endsWith('/content')){expect(route.request().headers().authorization).toBe('Bearer test');return route.fulfill({contentType:'text/plain',body:'原文'});}
 if(path==='/api/agents'&&method==='POST'){calls.binding=route.request().postDataJSON();return json({id:'a',...calls.binding as object,scope,scope_id:'personal',actions:{use:true,update:true,delete:true},skill_binding_ids:[],mcp_binding_ids:[]});}
 if(path.includes('capability-candidates'))return json([]);
 return json([]);
 });
 return calls;
}
test('knowledge management upload retry search original and delete',async({page},testInfo)=>{
 const calls=await setup(page);await page.goto('/workspace/knowledge-bases');
 await page.getByRole('button',{name:/制度库/}).click();
 await expect(page.getByText('未提取到文本')).toBeVisible();
 await page.getByLabel('上传文档',{exact:true}).setInputFiles({name:'新文档.txt',mimeType:'text/plain',buffer:Buffer.from('制度')});
 await expect.poll(()=>calls.upload).toBe(true);
 await page.getByRole('button',{name:'重试',exact:true}).click();
 await expect.poll(()=>calls.retry).toBe(true);
 await expect(page.getByText('可用 · 2 个片段')).toBeVisible();
 await page.getByLabel('检索问题').fill('如何请假');await page.getByRole('button',{name:'检索',exact:true}).click();
 await expect(page.getByText('请假须提前申请。')).toBeVisible();
 await page.screenshot({path:testInfo.outputPath('knowledge-detail.png'),fullPage:true});
 const download=page.waitForEvent('download');await page.getByRole('button',{name:'下载原文'}).click();await download;
 await page.getByRole('button',{name:'删除文档'}).click();expect(calls.deleted).toBe(false);await page.getByRole('button',{name:'确认删除',exact:true}).click();
 await expect(page.getByText('尚未上传文档。')).toBeVisible();
});
test('agent and knowledge catalogs center content and keep create action compact',async({page})=>{
 await setup(page);
 await page.setViewportSize({width:1920,height:900});
 for(const path of ['/workspace/agents','/workspace/knowledge-bases']){
  await page.goto(path);
  await expect(page.getByRole('heading',{name:'资源中心',exact:true})).toBeVisible();
  const categories=page.getByRole('group',{name:path.endsWith('agents')?'智能体分类':'知识库分类',exact:true});
  const content=categories.locator('..').locator('..').locator('..');
  const container=content.locator('..');
  const box=await content.boundingBox();
  const outer=await container.boundingBox();
  expect(box).not.toBeNull();
  expect(outer).not.toBeNull();
  expect(box!.width).toBeLessThanOrEqual(1180);
  expect(Math.abs(box!.x+box!.width/2-(outer!.x+outer!.width/2))).toBeLessThan(2);
 }
 await expect(page.getByRole('button',{name:'个人知识库',exact:true})).toBeVisible();
 await expect(page.getByRole('button',{name:'共享给我'})).toBeVisible();
 await expect(page.getByRole('button',{name:'团队知识库'})).toHaveCount(0);
 const create=page.getByRole('button',{name:'创建个人知识库'});
 const header=page.getByRole('group',{name:'知识库分类',exact:true}).locator('..').locator('..');
 expect((await create.boundingBox())!.height).toBeLessThan(50);
 expect((await create.boundingBox())!.height).toBeLessThan((await header.boundingBox())!.height);
 await page.setViewportSize({width:390,height:844});
 await page.reload();
 expect((await create.boundingBox())!.height).toBeLessThan(50);
 expect((await create.boundingBox())!.width).toBeLessThan(200);
});
test('resource cards and detail drawers share desktop and mobile dimensions',async({page},testInfo)=>{
 await setup(page);
 const sampleAgent={id:'a',slug:'writer',display_name:'写作助手',description:'整理写作资料',scope_id:'personal',scope:{id:'personal',name:'个人',kind:'personal'},actions:{use:true,update:true,delete:true},skill_binding_ids:[],mcp_binding_ids:[],unavailable_capability_ids:[],is_default:false};
 await page.route(/\/api\/agents(?:\?.*)?$/,route=>route.fulfill({json:resourceResponse(route.request().url(),[sampleAgent])}));
 await page.route('**/api/agents/a',route=>route.fulfill({json:sampleAgent}));
 const sampleSkill={id:'s',name:'reports',display_name:'报告整理',scope_id:'dept',scope_kind:'department',enabled:true,manageable:false,metadata:{description:'整理会议记录与行动事项。'.repeat(30)}};
 await page.route(/\/api\/skills(?:\?.*)?$/,route=>route.fulfill({json:resourceResponse(route.request().url(),[sampleSkill])}));
 for(const width of [1920,390]){
  await page.setViewportSize({width,height:900});
  let cardWidth:number|undefined;
  for(const [path,name] of [['agents','写作助手'],['knowledge-bases','制度库'],['skills','报告整理']]){
   await page.goto(`/workspace/${path}`);
   const card=page.getByRole('button',{name:new RegExp(name)});
   await expect(card).toBeVisible();
   const box=(await card.boundingBox())!;
   expect(box.height).toBe(240);
   if(cardWidth!==undefined)expect(Math.abs(box.width-cardWidth)).toBeLessThan(1);
   cardWidth=box.width;
   await card.click();
   const dialog=page.getByRole('dialog');
   await expect(dialog).toBeVisible();
   await expect.poll(async()=>Math.round((await dialog.boundingBox())!.width)).toBe(Math.min(width,760));
   await page.screenshot({path:testInfo.outputPath(`${path}-${width}.png`)});
   await page.keyboard.press('Escape');
   await expect(dialog).toHaveCount(0);
   await expect(card).toBeFocused();
  }
 }
});
test('mobile agent binding defaults empty and sends selected static mode',async({page},testInfo)=>{
 const calls=await setup(page);await page.setViewportSize({width:390,height:844});await page.goto('/workspace/agents');
 await page.getByRole('button',{name:'创建个人智能体'}).click();
 await expect(page.getByLabel('由 Agent 自主决定何时检索')).toHaveCount(0);
 await page.getByLabel(/制度库/).check();await expect(page.getByLabel('由 Agent 自主决定何时检索')).toBeChecked();
 await page.getByLabel('由 Agent 自主决定何时检索').uncheck();
 await page.screenshot({path:testInfo.outputPath('mobile-binding.png'),fullPage:true});await page.getByLabel('显示名称').fill('制度助手');await page.getByRole('button',{name:'创建智能体',exact:true}).click();
 await expect.poll(()=>calls.binding).toMatchObject({knowledge_config:{knowledge_base_ids:['kb'],parameters:{mode:'static',top_k:5}}});
});
