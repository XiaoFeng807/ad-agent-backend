// Real browser and model: two turns on one task, then approve the first.
const assert = require('node:assert/strict');
const {spawn} = require('node:child_process');
const {once} = require('node:events');
const fs = require('node:fs/promises');
const path = require('node:path');
const readline = require('node:readline');
const {chromium} = require('playwright');
const {browserOptions} = require('./browser_options.cjs');
async function main(){
  const [, , root, artifact] = process.argv;
  await fs.mkdir(root, {recursive:true});
  const dir = await fs.mkdtemp(path.join(root, 'run-'));
  const child = spawn(process.env.PYTHON || 'python', ['-B','-X','utf8','-m','scripts.run_natural_diagnosis','--serve','anomaly','--output-dir',dir,'--plan-metrics-db',artifact], {windowsHide:true, stdio:['pipe','pipe','ignore']});
  const report = {dir, ok:false};
  let browser, page;
  try {
    const origin = await new Promise((resolve,reject) => {
      const timer = setTimeout(() => reject(Error('server timeout')),90000);
      child.once('exit', code => {clearTimeout(timer); reject(Error('server exit '+code));});
      readline.createInterface({input:child.stdout}).on('line', line => {if(line.startsWith('DEMO_URL=')){clearTimeout(timer); resolve(line.slice(9));}});
    });
    browser = await chromium.launch(browserOptions());
    report.browser = browser.version();
    page = await browser.newPage(); page.setDefaultTimeout(60000);
    await page.goto(origin); await page.waitForFunction(() => window._captchaId);
    await page.locator('#loginUser').fill('diagnosis-demo'); await page.locator('#loginPass').fill('demo-password');
    await page.locator('#loginCaptcha').fill('0726'); await page.locator('#loginCaptcha').press('Enter');
    await page.locator('#navUsername').getByText('diagnosis-demo', {exact:true}).waitFor();
    await page.locator('[data-page="chat"]').click(); await page.getByRole('button',{name:'广告计划复核',exact:true}).click();
    async function submit(message){
      await page.getByRole('textbox',{name:'诊断需求'}).fill(message);
      const pending = page.waitForResponse(r => r.request().method()==='POST' && new URL(r.url()).pathname==='/api/chat/stream');
      await page.locator('#diagnosisSubmit').click();
      const response=await pending; assert.equal(response.status(),200);
      return (await response.text()).split('\n\n').filter(x=>x.startsWith('data: ')).map(x=>JSON.parse(x.slice(6)));
    }
    report.first = await submit('帮我找出最近点击率下滑最明显、值得暂停复核的一条投放计划。');
    await page.getByRole('button',{name:'基于此轮续问'}).first().click();
    report.second = await submit('继续这次比较，改成最近三天和之前三天');
    const a=report.first.at(-1), b=report.second.at(-1);
    assert.equal(a.type,'done'); assert.equal(b.type,'done'); assert.equal(a.task_id,b.task_id);
    assert.notEqual(a.turn_id,b.turn_id); assert.notEqual(a.trace_id,b.trace_id); assert.ok(a.review_proposal);
    const wait=(method,re)=>page.waitForResponse(r=>r.request().method()===method && re.test(new URL(r.url()).pathname));
    const post=wait('POST',/\/review-proposals\/[^/]+\/confirm$/), get=wait('GET',/\/diagnosis\/reviews\/[^/]+$/);
    await page.getByRole('button',{name:'确认创建本地复核单'}).first().click();
    const p=await post, g=await get; assert.equal(p.status(),200); assert.equal(g.status(),200);
    report.review_post=(await p.json()).data; report.review_get=(await g.json()).data;
    report.review_get_path=new URL(g.url()).pathname; assert.deepEqual(report.review_post,report.review_get);
    assert.equal(report.review_get.source_trace_id,a.trace_id); assert.equal(report.review_get.turn_id,a.turn_id);
    report.ok=true;
  } catch(error){report.failure=error.stack;}
  finally {
    await page?.close(); await browser?.close();
    if(child.exitCode===null && child.signalCode===null){const exited=once(child,'exit'); child.stdin.end(); const timer=setTimeout(()=>child.kill(),35000); try{await exited;}finally{clearTimeout(timer);}}
  }
  await fs.writeFile(path.join(dir,'task24.json'),JSON.stringify(report,null,2)+'\n');
  console.log(JSON.stringify({dir,ok:report.ok,failure:report.failure})); process.exitCode=report.ok?0:1;
}
main().catch(error=>{console.error(error);process.exitCode=1;});
