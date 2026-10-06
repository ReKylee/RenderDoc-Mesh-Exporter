import * as THREE from 'three';
import { OrbitControls } from 'three/addons/controls/OrbitControls.js';
import { GLTFLoader } from 'three/addons/loaders/GLTFLoader.js';
import { GLTFExporter } from 'three/addons/exporters/GLTFExporter.js';

const $ = id => document.getElementById(id);
new ResizeObserver(([entry])=>document.documentElement.style.setProperty('--header-height',entry.contentRect.height+'px')).observe(document.querySelector('header'));
const catalog = window.MeshCatalog;
const models = catalog.models;
const byId = new Map(models.map(m => [m.id, m]));
const loader = new GLTFLoader();
const scriptLoads = new Map();
const thumbCache = new Map();
const pendingThumbs = [];
let thumbnailBusy = false, filtered = [], activeId = null, activeRoot = null, mainRenderer, controls;
let openGeneration = 0, previewGeneration = 0;
let fsConnected=false, currentFolder='', folderTree=null;
const selectedIds=new Set(); let lastSelectedId=null;
let assignments = { groups: {} };
const storageKey = 'renderdoc-mesh-roles:' + catalog.capture;
try { assignments = JSON.parse(localStorage.getItem(storageKey)) || assignments; } catch (_) {}
if (!assignments.groups) assignments = { groups: {} };
const error = message => { $('error').textContent = String(message); };
const saveLocal = () => { try { localStorage.setItem(storageKey, JSON.stringify(assignments)); } catch (_) {} };

function download(blob, filename) {
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a'); a.href = url; a.download = filename; a.click();
  setTimeout(() => URL.revokeObjectURL(url), 5000);
}
function option(value, text) {
  const o = document.createElement('option'); o.value = value; o.textContent = text; return o;
}
function roleState(model) { return { ...model.material, ...(assignments.groups[model.textureGroup] || {}) }; }
function bindingLabel(b) {
  return `${b.guestSlot === undefined ? 'Binding ' + b.bindingIndex : 'tf' + b.guestSlot} | ${b.width}x${b.height} | ${b.resourceName || b.resource}`;
}
function getPayload(id) {
  if (window.MeshData[id]) return Promise.resolve(window.MeshData[id]);
  if (!scriptLoads.has(id)) scriptLoads.set(id, new Promise((resolve, reject) => {
    const s = document.createElement('script'); s.src = 'models/' + id + '.js';
    s.onload = () => window.MeshData[id] ? resolve(window.MeshData[id]) : reject(new Error('Missing mesh payload: ' + id));
    s.onerror = () => reject(new Error('Could not read ' + s.src)); document.head.append(s);
  }));
  return scriptLoads.get(id);
}
async function loadRoot(model) {
  const encoded = await getPayload(model.id);
  const raw = atob(encoded); const bytes = new Uint8Array(raw.length);
  for (let i = 0; i < raw.length; i++) bytes[i] = raw.charCodeAt(i);
  const gltf = await loader.parseAsync(bytes.buffer, '');
  const root = gltf.scene;
  root.traverse(node => {
    if (node.geometry) {
      node.__sourceUVs = {};
      for (const [key, value] of Object.entries(node.geometry.attributes)) {
        if (/^uv\d*$/.test(key)) node.__sourceUVs[key] = value;
        const raw = /^texcoord_(\d+)$/.exec(key);
        if (raw) node.__sourceUVs[Number(raw[1])===0?'uv':'uv'+Number(raw[1])] = value;
      }
    }
  });
  return root;
}
function dispose(root) {
  if (!root) return;
  const geometries = new Set(), materials = new Set(), textures = new Set();
  root.traverse(n => {
    if (n.geometry) geometries.add(n.geometry);
    for (const m of (Array.isArray(n.material) ? n.material : n.material ? [n.material] : [])) materials.add(m);
  });
  for (const m of materials) for (const v of Object.values(m)) if (v?.isTexture) textures.add(v);
  for (const t of textures) { t.dispose(); t.source?.data?.close?.(); }
  for (const m of materials) m.dispose();
  for (const g of geometries) g.dispose();
}
function disposeMaterials(root) {
  const seen = new Set(), textures = new Set();
  root.traverse(n => { for (const m of (Array.isArray(n.material) ? n.material : n.material ? [n.material] : [])) seen.add(m); });
  for (const m of seen) {
    for (const v of Object.values(m)) if (v?.isTexture) textures.add(v);
    m.dispose();
  }
  for (const t of textures) { t.dispose(); t.source?.data?.close?.(); }
}
async function texture(row, color, flipV, flipNormalY = false) {
  if (!row?.image || !window.MeshImages[row.image]) return null;
  const result = await new THREE.TextureLoader().loadAsync(window.MeshImages[row.image]);
  if (flipNormalY) {
    const canvas=document.createElement('canvas'); canvas.width=result.image.width; canvas.height=result.image.height;
    const ctx=canvas.getContext('2d'); ctx.drawImage(result.image,0,0); const pixels=ctx.getImageData(0,0,canvas.width,canvas.height);
    for(let i=1;i<pixels.data.length;i+=4)pixels.data[i]=255-pixels.data[i];
    ctx.putImageData(pixels,0,0); result.image=canvas;
  }
  result.flipY = false; result.wrapS = result.wrapT = THREE.RepeatWrapping;
  result.colorSpace = color ? THREE.SRGBColorSpace : THREE.NoColorSpace;
  if (flipV) { result.repeat.y = -1; result.offset.y = 1; }
  result.needsUpdate = true; return result;
}
async function applyMaterial(root, model, shading = 'lit', wire = false) {
  const state = roleState(model);
  const base = model.textures.find(b => b.key === state.base_color);
  const normal = model.textures.find(b => b.key === state.normal);
  const [map, normalMap] = await Promise.all([texture(base, true, state.flip_v), texture(normal, false, state.flip_v, state.flip_normal_y)]);
  disposeMaterials(root);
  const key = n => Number(n) === 0 ? 'uv' : 'uv' + Number(n);
  const baseUv = state.base_uv ?? base?.uvSet ?? 0, normalUv = state.normal_uv ?? normal?.uvSet ?? 0;
  if (map) map.channel = 0;
  if (normalMap) normalMap.channel = 1;
  root.traverse(node => {
    if (!node.isMesh) return;
    const uv = node.__sourceUVs;
    if (uv[key(baseUv)]) node.geometry.setAttribute('uv', uv[key(baseUv)]);
    if (uv[key(normalUv)]) node.geometry.setAttribute('uv1', uv[key(normalUv)]);
    const settings = { side: THREE.DoubleSide, wireframe: wire };
    if (shading === 'normals') node.material = new THREE.MeshNormalMaterial(settings);
    else if (shading === 'unlit') node.material = new THREE.MeshBasicMaterial({ ...settings, map: uv[key(baseUv)] ? map : null, color: map ? 0xffffff : 0xadb7c7 });
    else node.material = new THREE.MeshStandardMaterial({ ...settings, color: map ? 0xffffff : 0xadb7c7,
      map: uv[key(baseUv)] ? map : null, normalMap: uv[key(normalUv)] ? normalMap : null,
      normalScale: new THREE.Vector2(1, 1), metalness: 0, roughness: 0.85 });
  });
  if (shading === 'normals') { map?.dispose(); normalMap?.dispose(); }
  else if (shading === 'unlit') normalMap?.dispose();
}
function world() {
  const scene = new THREE.Scene(); scene.background = new THREE.Color(0x171b23);
  scene.add(new THREE.HemisphereLight(0xffffff, 0x879bb7, 2));
  const key = new THREE.DirectionalLight(0xffffff, 2.4); key.position.set(4, 8, 5); scene.add(key);
  const fill = new THREE.DirectionalLight(0xc6d9ff, 0.6); fill.position.set(-5, 2, -3); scene.add(fill);
  return scene;
}
function frame(root, camera, aspect, up = 'Y') {
  root.position.set(0, 0, 0); root.updateMatrixWorld(true);
  const box = new THREE.Box3().setFromObject(root), center = box.getCenter(new THREE.Vector3());
  const size = box.getSize(new THREE.Vector3());
  if (!Number.isFinite(size.length())) throw new Error('Invalid mesh bounds');
  root.position.sub(center); root.updateMatrixWorld(true);
  const radius = Math.max(size.length() * 0.5, 1e-4);
  const fov = THREE.MathUtils.degToRad(camera.fov), fit = Math.min(fov, 2 * Math.atan(Math.tan(fov/2) * aspect));
  const distance = radius / Math.sin(fit/2) * 1.08;
  camera.up.set(0, up === 'Y' ? 1 : 0, up === 'Z' ? 1 : 0);
  const direction = up === 'Y' ? new THREE.Vector3(1, 0.55, 1.3) : new THREE.Vector3(1, -1.3, 0.65);
  camera.position.copy(direction.normalize().multiplyScalar(distance));
  camera.near = Math.max(radius / 1000, 1e-6); camera.far = distance + radius * 10;
  camera.aspect = aspect; camera.lookAt(0,0,0); camera.updateProjectionMatrix();
}
let thumbRenderer;
try { thumbRenderer = new THREE.WebGLRenderer({ antialias: true, preserveDrawingBuffer: true }); thumbRenderer.setSize(480,360); thumbRenderer.outputColorSpace = THREE.SRGBColorSpace; }
catch (e) { error('WebGL unavailable: ' + e.message + '. Raw mesh and texture links remain usable.'); }
async function renderThumbnail(model) {
  if (!thumbRenderer) return null;
  const root = await loadRoot(model), scene = world();
  try {
    await applyMaterial(root, model, 'lit'); scene.add(root);
    const camera = new THREE.PerspectiveCamera(35, 4/3, .01, 10000); frame(root, camera, 4/3);
    thumbRenderer.render(scene, camera); return thumbRenderer.domElement.toDataURL('image/png');
  } finally { scene.remove(root); dispose(root); }
}
async function drainThumbs() {
  if (thumbnailBusy) return; thumbnailBusy = true;
  while (pendingThumbs.length) {
    const { model, image, state, generation } = pendingThumbs.shift();
    if (!image.isConnected || generation !== previewGeneration) continue;
    try {
      const result = thumbCache.get(model.id) || await renderThumbnail(model);
      if (result && generation === previewGeneration) { thumbCache.set(model.id, result); image.src = result; state.textContent = ''; }
    } catch (e) { state.textContent = 'Preview failed'; image.alt = e.message; }
    await new Promise(resolve => setTimeout(resolve, 0));
  }
  thumbnailBusy = false;
}
const observer = new IntersectionObserver(entries => {
  for (const entry of entries) if (entry.isIntersecting) {
    const el = entry.target; observer.unobserve(el);
    pendingThumbs.push({ model: byId.get(el.dataset.id), image: el.querySelector('img'), state: el.querySelector('.preview-state'), generation: previewGeneration });
  }
  drainThumbs();
}, { rootMargin: '500px' });
function renderGrid() {
  observer.disconnect(); previewGeneration++; pendingThumbs.length = 0;
  const search = $('search').value.toLowerCase().trim();
  filtered = models.filter(m => m.vertexCount >= Number($('min-vertices').value || 1) && (!fsConnected || (m._fsPresent && (m.folderPath||'')===currentFolder)));
  if ($('source-filter').value) filtered = filtered.filter(m => m.adapter === $('source-filter').value);
  if ($('unique').checked) filtered = filtered.filter(m => !m.duplicateOf);
  if ($('geometry-filter').value === 'triangles') filtered = filtered.filter(m => m.triangleCount > 0);
  if ($('geometry-filter').value === 'lines') filtered = filtered.filter(m => !m.triangleCount);
  if ($('group-filter').value) filtered = filtered.filter(m => m.textureGroup === $('group-filter').value);
  if (search) filtered = filtered.filter(m => (m.id + ' ' + (m.displayName||'') + ' ' + (m.folderName||'') + ' ' + m.textures.map(b => bindingLabel(b)).join(' ')).toLowerCase().includes(search));
  filtered.sort((a,b) => $('sort').value === 'vertices' ? b.vertexCount-a.vertexCount || a.eventId-b.eventId : $('sort').value === 'triangles' ? b.triangleCount-a.triangleCount || a.eventId-b.eventId : a.eventId-b.eventId || a.instance-b.instance);
  const total=fsConnected?models.filter(m=>m._fsPresent).length:models.length;
  $('summary').textContent = `${filtered.length} shown / ${total} models`;
  $('grid').replaceChildren();
  for (const m of filtered) {
    const card = document.createElement('article'); card.className = 'card'+(selectedIds.has(m.id)?' selected':''); card.dataset.id=m.id; card.draggable=fsConnected;
    card.addEventListener('click',e=>{if(e.target.closest('a,button,select,input'))return;selectModel(m.id,e);});
    card.addEventListener('dblclick',e=>{if(!e.target.closest('a,button,select,input')){e.preventDefault();openModel(m.id)}});
    card.addEventListener('dragstart',e=>beginModelDrag(e,m.id)); card.addEventListener('dragend',()=>card.classList.remove('dragging'));
    const thumb = document.createElement('div'); thumb.className = 'thumb'; thumb.dataset.id = m.id; thumb.setAttribute('role','group');
    thumb.setAttribute('aria-label', `Event ${m.eventId}, instance ${m.instance} preview`);
    const image = document.createElement('img'); image.src='data:image/gif;base64,R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAALAAAAAABAAEAAAIBRAA7'; image.alt = `Event ${m.eventId} source-space mesh`; image.width = 480; image.height = 360;
    const open = document.createElement('button'); open.className='open3d'; open.type='button'; open.textContent='Open 3D'; open.onclick=e=>{e.preventDefault();e.stopPropagation();openModel(m.id)};
    const state = document.createElement('span'); state.className='preview-state'; state.textContent='Rendering preview…';
    thumb.append(image,open,state); card.append(thumb);
    const info = document.createElement('div'); info.className = 'details';
    const title = document.createElement('h2'); title.textContent = m.displayName || m.folderName || `Event ${m.eventId} / instance ${m.instance}`;
    const stats = document.createElement('div'); stats.className = 'stats';
    const uv = m.uvSets.length;
    stats.textContent = `${m.vertexCount.toLocaleString()} vertices | ${m.triangleCount.toLocaleString()} triangles | ${uv} UV sets | ${m.textures.length} texture bindings`;
    const space = document.createElement('div'); space.className = /projected|clip/.test(m.space) ? 'bad' : 'stats'; space.textContent = m.space;
    const links = document.createElement('div'); links.className = 'links';
    for (const [name,path] of [['GLB',m.path],['OBJ',m.obj],['Metadata',m.metadata]]) { const a = document.createElement('a'); a.textContent=name; a.href='../'+path; links.append(a); }
    info.append(title,stats,space,links); card.append(info); $('grid').append(card);
    if (thumbCache.has(m.id)) { image.src=thumbCache.get(m.id); state.textContent=''; } else observer.observe(thumb);
  }
  if (!filtered.length) { const p=document.createElement('p');p.className='empty';p.textContent='No meshes match these filters.';$('grid').append(p); }
}
const mainScene = world(), camera = new THREE.PerspectiveCamera(40, 1, .01, 10000);
function ensureMain() {
  if (mainRenderer) return;
  mainRenderer = new THREE.WebGLRenderer({ antialias:true, preserveDrawingBuffer:true });
  mainRenderer.setPixelRatio(Math.min(devicePixelRatio,2)); mainRenderer.outputColorSpace=THREE.SRGBColorSpace;
  $('stage').prepend(mainRenderer.domElement); controls=new OrbitControls(camera,mainRenderer.domElement); controls.enableDamping=true;
  mainRenderer.setAnimationLoop(() => { if ($('model-dialog').open) { resize(); controls.update(); mainRenderer.render(mainScene,camera); } });
}
function resize() {
  if (!mainRenderer) return;
  const width=$('stage').clientWidth, height=$('stage').clientHeight;
  if (!width || !height) return;
  const size=mainRenderer.getSize(new THREE.Vector2());
  if (size.x!==width || size.y!==height) { mainRenderer.setSize(width,height,false);camera.aspect=width/height;camera.updateProjectionMatrix(); }
}
function frameMain() {
  if (!activeRoot) return; resize(); frame(activeRoot,camera,$('stage').clientWidth/Math.max(1,$('stage').clientHeight),$('up').value);
  controls.target.set(0,0,0);controls.update();
}
function setupRoleUI(model) {
  const state=roleState(model), group=catalog.groups[model.textureGroup];
  $('group-title').textContent=model.textureGroup==='no_textures'?'No sampled textures':'Texture group '+model.textureGroup.slice(0,8);
  $('group-info').textContent=`${group.models.length} draw export(s) use this texture set. Current event: ${model.eventId}.`;
  for (const [id,role,uvId,uvRole] of [['base-role','base_color','base-uv','base_uv'],['normal-role','normal','normal-uv','normal_uv']]) {
    const select=$(id);select.replaceChildren(option('','None'));
    for (const b of model.textures) if (b.image) select.append(option(b.key,bindingLabel(b)+' ['+b.role+']'));
    select.value=state[role]||'';
    const uv=$(uvId);uv.replaceChildren(option('auto','Captured per-draw UV'),...model.uvSets.map(k=>option(k.split('_')[1],k)));
    const chosen=model.textures.find(b=>b.key===state[role]);uv.value=state[uvRole]===undefined?'auto':String(state[uvRole]);
  }
  $('normal-flip').checked=!!state.flip_normal_y;$('uv-flip').checked=!!state.flip_v;
  $('normal-source').textContent='Vertex normals: '+(typeof model.normalSource==='string'?model.normalSource:JSON.stringify(model.normalSource));
  $('raw-glb').href='../'+model.path;$('raw-metadata').href='../'+model.metadata;
  $('texture-list').replaceChildren();
  for (const b of model.textures) {
    const row=document.createElement('div');row.className='texture-row';
    const a=document.createElement('a');a.href=b.image?'../'+b.image:'#';a.target='_blank';a.rel='noopener';
    const img=document.createElement('img');img.alt=bindingLabel(b);if (b.image)img.src=window.MeshImages[b.image];a.append(img);
    const text=document.createElement('div');const name=document.createElement('strong');name.textContent=bindingLabel(b);
    const detail=document.createElement('p');detail.textContent=`${b.role}; ${b.roleEvidence}. ${b.uvSet===null?'No proven mesh-UV association':'TEXCOORD_'+b.uvSet}. ${b.error||''}`;
    text.append(name,detail);row.append(a,text);$('texture-list').append(row);
  }
}
async function openModel(id) {
  activeId=id;const token=++openGeneration;const model=byId.get(id);
  if (!$('model-dialog').open)$('model-dialog').showModal();
  $('model-title').textContent=model.displayName||model.folderName||`Event ${model.eventId} / instance ${model.instance}`;$('model-status').textContent='Loading model...';setupRoleUI(model);
  try {
    ensureMain();const root=await loadRoot(model);
    await applyMaterial(root,model,$('shading').value,$('wireframe').checked);
    if (token!==openGeneration) {dispose(root);return;}
    if (activeRoot) {mainScene.remove(activeRoot);dispose(activeRoot);}
    activeRoot=root;mainScene.add(root);frameMain();
    $('model-status').textContent=model.space+(model.material.base_color?' | Preview material uses editable captured texture assignments':' | Choose a texture from this draw group to assign a material');
  } catch(e) {$('model-status').textContent=e.message;console.error(e);}
}
function closeModel() {++openGeneration;$('model-dialog').close();if(activeRoot){mainScene.remove(activeRoot);dispose(activeRoot);activeRoot=null;}activeId=null;}
function browse(delta) { const i=filtered.findIndex(m=>m.id===activeId);if(i>=0&&filtered.length)openModel(filtered[(i+delta+filtered.length)%filtered.length].id); }
async function updateRoles() {
  if (!activeId)return;const model=byId.get(activeId);
  assignments.groups[model.textureGroup]={base_color:$('base-role').value,normal:$('normal-role').value,
    base_uv:Number($('base-uv').value),normal_uv:Number($('normal-uv').value),flip_normal_y:$('normal-flip').checked,flip_v:$('uv-flip').checked};
  if($('base-uv').value==='auto')delete assignments.groups[model.textureGroup].base_uv;
  if($('normal-uv').value==='auto')delete assignments.groups[model.textureGroup].normal_uv;
  saveLocal();for(const m of models)if(m.textureGroup===model.textureGroup)thumbCache.delete(m.id);
  renderGrid();await openModel(activeId);
}
for(const id of ['search','source-filter','geometry-filter','sort','min-vertices','unique','group-filter'])$(id).addEventListener('input',renderGrid);
for(const id of ['base-role','base-uv','normal-role','normal-uv','normal-flip','uv-flip'])$(id).addEventListener('change',updateRoles);
for(const id of ['shading','wireframe'])$(id).addEventListener('change',()=>activeId&&openModel(activeId));
$('up').onchange=frameMain;$('frame').onclick=frameMain;$('close').onclick=closeModel;
$('model-dialog').addEventListener('cancel',e=>{e.preventDefault();closeModel();});$('previous').onclick=()=>browse(-1);$('next').onclick=()=>browse(1);
document.addEventListener('keydown',e=>{if(!$('model-dialog').open||['INPUT','SELECT','TEXTAREA'].includes(e.target.tagName))return;if(e.key==='ArrowLeft')browse(-1);if(e.key==='ArrowRight')browse(1);});
$('reset-roles').onclick=()=>{if(!activeId)return;const model=byId.get(activeId);delete assignments.groups[model.textureGroup];saveLocal();for(const m of models)if(m.textureGroup===model.textureGroup)thumbCache.delete(m.id);renderGrid();openModel(activeId);};
$('save-png').onclick=()=>{if(!activeRoot)return;mainRenderer.render(mainScene,camera);mainRenderer.domElement.toBlob(b=>b&&download(b,activeId+'.png'),'image/png');};
$('save-glb').onclick=async()=>{
  if(!activeId)return;const model=byId.get(activeId);let root;
  try{root=await loadRoot(model);await applyMaterial(root,model,'lit');
    root.userData={capture:catalog.capture,event:model.eventId,textureGroup:model.textureGroup};
    const b=await new GLTFExporter().parseAsync(root,{binary:true});
    download(new Blob([b],{type:'model/gltf-binary'}),model.id+'_assigned.glb');
  }catch(e){error(e.message);}finally{dispose(root);}
};
$('show-group').onclick=()=>{if(!activeId)return;const group=byId.get(activeId).textureGroup;closeModel();$('group-filter').value=group;renderGrid();};
$('title').textContent=catalog.capture+' | mesh gallery';
for(const adapter of new Set(models.map(m=>m.adapter).filter(Boolean)))$('source-filter').append(option(adapter,adapter));
if(models.some(m=>m.adapter==='Xenia DXBC guest vfetch'))$('source-filter').value='Xenia DXBC guest vfetch';
for(const group of Object.values(catalog.groups))$('group-filter').append(option(group.id,(group.id==='no_textures'?'No textures':group.id.slice(0,8))+` (${group.models.length} exports)`));
$('status').textContent=`${catalog.summary.uniqueImages} texture images, ${catalog.summary.textureGroups} texture groups. `+(catalog.summary.unsupported?`${catalog.summary.unsupported} unsupported draw(s). `:'')+'Click to select, Shift/Ctrl-click for multi-select, Ctrl+A selects this folder/filter, and double-click opens 3D.';
// Initial cards are rendered only after the filesystem scan, avoiding a throwaway DOM rebuild.
// A small public testing hook also permits saving thumbnails without screen scraping.
// MeshBrowser is exposed after organizer initialization.


let filesystemSignature='';
function filesystemSignatureOf(data){return JSON.stringify([data.tree,(data.models||[]).map(m=>[m.id,m.folderPath,m.folderName,m.displayName,m.path,m.obj])])}

async function api(path, body) {
  const response = await fetch(path, {
    method: body === undefined ? 'GET' : 'POST',
    headers: body === undefined ? {} : {'Content-Type':'application/json'},
    body: body === undefined ? undefined : JSON.stringify(body),
    cache: 'no-store'
  });
  const data = await response.json();
  if (!response.ok || data.error) throw new Error(data.error || ('Request failed: ' + response.status));
  return data;
}

function flattenFolders(node, out = []) {
  out.push(node);
  for (const child of node.children || []) flattenFolders(child, out);
  return out;
}
function folderPaths() {
  return new Set(flattenFolders(folderTree || {path:'', children:[]}).map(n => n.path));
}
function applyFilesystem(data, preserve = []) {
  fsConnected = true;
  filesystemSignature=filesystemSignatureOf(data);
  folderTree = data.tree;
  for (const m of models) {
    m._fsPresent = false;
    delete m.folderPath;
    delete m.folderName;
    delete m.displayName;
    delete m.modelPath;
  }
  for (const row of data.models || []) {
    const m = byId.get(row.id);
    if (!m) continue;
    Object.assign(m, row, {_fsPresent:true});
  }
  if (!folderPaths().has(currentFolder)) currentFolder = '';
  selectedIds.clear();
  for (const id of preserve) {
    const m = byId.get(id);
    if (m?._fsPresent && (m.folderPath || '') === currentFolder) selectedIds.add(id);
  }
  lastSelectedId = selectedIds.size ? Array.from(selectedIds).at(-1) : null;
  renderFolderTree();
  renderGrid();
  updateOrganizerUI();
}
async function refreshFilesystem(preserve = []) {
  try {
    applyFilesystem(await api('/api/tree'), preserve);
  } catch (e) {
    error('Filesystem sync failed: ' + e.message);
  }
}

function renderFolderTree() {
  const host = $('folder-tree');
  host.replaceChildren();
  for (const node of flattenFolders(folderTree || {name:'meshes', path:'', models:0, children:[]})) {
    const row = document.createElement('div');
    row.className = 'folder-row' + (node.path === currentFolder ? ' active' : '');
    row.dataset.path = node.path;
    const depth = node.path ? node.path.split('/').length : 0;
    const indent = document.createElement('span');
    indent.className = 'folder-depth';
    indent.style.setProperty('--depth', depth);
    const name = document.createElement('span');
    name.textContent = node.path ? node.name : 'meshes/';
    const count = document.createElement('span');
    count.className = 'count';
    count.textContent = String(node.models || 0);
    row.append(indent, name, count);
    row.onclick = () => {
      currentFolder = node.path;
      selectedIds.clear();
      lastSelectedId = null;
      renderFolderTree();
      renderGrid();
      updateOrganizerUI();
    };
    row.ondblclick = () => { if (node.path) renameFolder(node.path); };
    row.ondragover = e => { e.preventDefault(); row.classList.add('drop'); };
    row.ondragleave = () => row.classList.remove('drop');
    row.ondrop = async e => {
      e.preventDefault();
      row.classList.remove('drop');
      await moveSelectedTo(node.path);
    };
    host.append(row);
  }
  updateMoveTargets();
}
function updateMoveTargets() {
  const select = $('move-target');
  const previous = select.value;
  select.replaceChildren();
  for (const node of flattenFolders(folderTree || {name:'meshes', path:'', children:[]})) {
    const depth = node.path ? node.path.split('/').length : 0;
    const label = (depth ? '  '.repeat(depth) : '') + (node.path ? node.name : 'meshes/');
    select.append(option(node.path, label));
  }
  if ([...select.options].some(o => o.value === previous)) select.value = previous;
  else if (currentFolder) select.value = '';
  else if (select.options.length > 1) select.selectedIndex = 1;
}
function syncSelectionVisuals() {
  for (const card of $('grid').querySelectorAll('.card')) card.classList.toggle('selected', selectedIds.has(card.dataset.id));
}
function updateOrganizerUI() {
  const n = selectedIds.size;
  $('selection-bar').hidden = !n;
  $('selection-count').textContent = n === 1 ? '1 selected' : (n + ' selected');
  $('rename-selected').disabled = n !== 1;
  $('move-selected').disabled = !n;
  $('rename-folder').disabled = !currentFolder;
}
function selectModel(id, e = {}) {
  const ordered = filtered.map(m => m.id);
  const idx = ordered.indexOf(id);
  if (e.shiftKey && lastSelectedId && ordered.includes(lastSelectedId)) {
    if (!e.ctrlKey && !e.metaKey) selectedIds.clear();
    const a = ordered.indexOf(lastSelectedId);
    const lo = Math.min(a, idx), hi = Math.max(a, idx);
    for (let i = lo; i <= hi; i++) selectedIds.add(ordered[i]);
  } else if (e.ctrlKey || e.metaKey) {
    selectedIds.has(id) ? selectedIds.delete(id) : selectedIds.add(id);
    lastSelectedId = id;
  } else {
    selectedIds.clear();
    selectedIds.add(id);
    lastSelectedId = id;
  }
  syncSelectionVisuals();
  updateOrganizerUI();
}
function clearSelection() {
  selectedIds.clear();
  lastSelectedId = null;
  syncSelectionVisuals();
  updateOrganizerUI();
}
function beginModelDrag(e, id) {
  if (!selectedIds.has(id)) {
    selectedIds.clear();
    selectedIds.add(id);
    lastSelectedId = id;
    syncSelectionVisuals();
  }
  e.currentTarget.classList.add('dragging');
  e.dataTransfer.effectAllowed = 'move';
  e.dataTransfer.setData('text/plain', Array.from(selectedIds).join('\n'));
  updateOrganizerUI();
}
async function moveSelectedTo(target) {
  const chosen = models.filter(m => selectedIds.has(m.id) && m._fsPresent && (m.folderPath || '') !== target);
  if (!chosen.length) return;
  const ids = chosen.map(m => m.id);
  try {
    const data = await api('/api/models/move', {paths:chosen.map(m => m.modelPath), target});
    currentFolder = target;
    applyFilesystem(data, ids);
  } catch (e) {
    error(e.message);
    await refreshFilesystem();
  }
}
async function renameSelected() {
  if (selectedIds.size !== 1) return;
  const id = Array.from(selectedIds)[0];
  const model = byId.get(id);
  const next = prompt('Rename model', model.displayName || model.folderName);
  if (!next || next === model.folderName) return;
  try {
    const data = await api('/api/models/rename', {path:model.modelPath, name:next});
    applyFilesystem(data, [id]);
  } catch (e) {
    error(e.message);
  }
}
async function createFolder() {
  const next = prompt('New folder name');
  if (!next) return;
  try {
    applyFilesystem(await api('/api/folders', {parent:currentFolder, name:next}), Array.from(selectedIds));
  } catch (e) {
    error(e.message);
  }
}
async function renameFolder(path = currentFolder) {
  if (!path) return;
  const old = path.split('/').at(-1);
  const next = prompt('Rename folder', old);
  if (!next || next === old) return;
  try {
    const parent = path.includes('/') ? path.slice(0, path.lastIndexOf('/')) : '';
    const data = await api('/api/folders/rename', {path, name:next});
    currentFolder = parent ? parent + '/' + next : next;
    applyFilesystem(data, Array.from(selectedIds));
  } catch (e) {
    error(e.message);
  }
}

$('new-folder').onclick = createFolder;
$('rename-folder').onclick = () => renameFolder();
$('rename-selected').onclick = renameSelected;
$('move-selected').onclick = () => moveSelectedTo($('move-target').value);
$('clear-selection').onclick = clearSelection;

const organizerGrid = $('grid');
const selectionBox = $('selection-box');
let boxStart = null;
let boxInitial = new Set();
organizerGrid.addEventListener('pointerdown', e => {
  if (e.button !== 0 || e.target.closest('.card')) return;
  boxStart = {x:e.clientX, y:e.clientY, add:e.ctrlKey || e.metaKey};
  boxInitial = new Set(selectedIds);
  if (!boxStart.add) selectedIds.clear();
  selectionBox.style.display = 'block';
  organizerGrid.setPointerCapture(e.pointerId);
});
organizerGrid.addEventListener('pointermove', e => {
  if (!boxStart) return;
  const x = Math.min(boxStart.x, e.clientX);
  const y = Math.min(boxStart.y, e.clientY);
  const w = Math.abs(e.clientX - boxStart.x);
  const h = Math.abs(e.clientY - boxStart.y);
  Object.assign(selectionBox.style, {left:x+'px', top:y+'px', width:w+'px', height:h+'px'});
  if (boxStart.add) {
    selectedIds.clear();
    for (const id of boxInitial) selectedIds.add(id);
  }
  for (const card of organizerGrid.querySelectorAll('.card')) {
    const r = card.getBoundingClientRect();
    const hit = r.right > x && r.left < x+w && r.bottom > y && r.top < y+h;
    if (hit) selectedIds.add(card.dataset.id);
    else if (!boxStart.add) selectedIds.delete(card.dataset.id);
    card.classList.toggle('selected', selectedIds.has(card.dataset.id));
  }
  updateOrganizerUI();
});
organizerGrid.addEventListener('pointerup', e => {
  if (!boxStart) return;
  boxStart = null;
  selectionBox.style.display = 'none';
  try { organizerGrid.releasePointerCapture(e.pointerId); } catch {}
  lastSelectedId = selectedIds.size ? Array.from(selectedIds).at(-1) : lastSelectedId;
  updateOrganizerUI();
});
document.addEventListener('keydown', e => {
  if ($('model-dialog').open || ['INPUT','SELECT','TEXTAREA'].includes(e.target.tagName)) return;
  if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'a') {
    e.preventDefault();
    selectedIds.clear();
    for (const m of filtered) selectedIds.add(m.id);
    lastSelectedId = filtered.at(-1)?.id || null;
    syncSelectionVisuals();
    updateOrganizerUI();
  } else if (e.key === 'Escape' && selectedIds.size) {
    clearSelection();
  }
});

window.MeshBrowser = {openModel, renderThumbnail, models, summary:()=>$('summary').textContent,
  refreshFilesystem, selectModel, selectedIds, moveSelectedTo, renameSelected, createFolder};
refreshFilesystem().then(()=>{if(fsConnected)setInterval(async()=>{try{const data=await api('/api/tree');if(filesystemSignatureOf(data)!==filesystemSignature)applyFilesystem(data,Array.from(selectedIds))}catch{}},1500)});
