const fs = require('fs');
const path = require('path');
const net = require('net');
const http = require('http');
const { spawn } = require('child_process');
const { chromium } = require('playwright-core');

const output = path.resolve(process.argv[2] || '');
if (!process.argv[2] || !fs.existsSync(path.join(output, 'viewer', 'index.html'))) {
  console.error('Usage: node tests/test_browser.cjs <export-directory>'); process.exit(2);
}
const results = path.join(output, 'validation');
fs.mkdirSync(results, { recursive: true });

function freePort() {
  return new Promise((resolve, reject) => {
    const server = net.createServer();
    server.on('error', reject);
    server.listen(0, '127.0.0.1', () => {
      const port = server.address().port;
      server.close(() => resolve(port));
    });
  });
}
function ping(url) {
  return new Promise(resolve => {
    const req=http.get(url+'/api/tree',res=>{res.resume();resolve(res.statusCode===200)});
    req.on('error',()=>resolve(false));req.setTimeout(500,()=>{req.destroy();resolve(false)});
  });
}
async function waitServer(url) {
  for (let i=0;i<60;i++) {
    if(await ping(url))return;
    await new Promise(r=>setTimeout(r,100));
  }
  throw new Error('Viewer server did not start');
}

(async () => {
  const port = await freePort();
  const python = process.env.PYTHON_EXECUTABLE || 'python';
  const server = spawn(python, [path.join(output,'viewer','server.py'), output, '--no-browser', '--port', String(port)], {stdio:'ignore'});
  let browser;
  try {
    const url='http://127.0.0.1:'+port;
    await waitServer(url);
    browser = await chromium.launch({
      executablePath: process.env.BROWSER_EXECUTABLE || 'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe',
      headless: true, args: ['--enable-webgl', '--ignore-gpu-blocklist']
    });
    const page = await browser.newPage({ viewport: { width: 1440, height: 1000 } });
    const errors = [];
    page.on('pageerror', e => errors.push(e.message));
    page.on('console', m => { if (m.type()==='error') errors.push(m.text()); });
    await page.goto(url+'/');
    await page.waitForFunction(() => window.MeshBrowser && document.querySelector('.card'), null, {timeout:60000});
    const models = await page.evaluate(() => window.MeshBrowser.models);
    await page.waitForFunction(() => document.querySelector('#folder-tree .folder-row'));
    await page.locator('#title').click();
    await page.keyboard.press('Control+A');
    const selected = await page.evaluate(() => window.MeshBrowser.selectedIds.size);
    const visible = await page.locator('.card').count();
    if (selected !== visible) throw new Error('Ctrl+A did not select the current visible scope');

    const sample = models.filter(m=>m.material?.normal&&m.material?.base_color).sort((a,b)=>b.vertexCount-a.vertexCount)[0]
      || models.find(m=>m.material?.base_color) || models[0];
    await page.evaluate(id => window.MeshBrowser.openModel(id), sample.id);
    await page.waitForFunction(() => document.querySelector('#model-dialog').open && !document.querySelector('#model-status').textContent.includes('Loading'));
    await page.screenshot({ path: path.join(results, 'model.png') });

    const downloadPromise = page.waitForEvent('download', { timeout:30000 });
    await page.locator('#save-glb').click();
    const download = await downloadPromise;
    await download.saveAs(path.join(results, 'assigned.glb'));
    const glb = fs.readFileSync(path.join(results, 'assigned.glb'));
    if (glb.readUInt32LE(0)!==0x46546c67 || glb.readUInt32LE(8)!==glb.length) throw new Error('Invalid assigned GLB');

    const report = {models:models.length, visible, ctrlA:selected, sample:sample.id, assignedGlbBytes:glb.length, errors};
    fs.writeFileSync(path.join(results,'browser_test.json'),JSON.stringify(report,null,2));
    if (errors.length) throw new Error(errors.join('\n'));
    console.log(JSON.stringify(report,null,2));
  } finally {
    if (browser) await browser.close();
    server.kill();
  }
})().catch(e => { console.error(e); process.exitCode=1; });
