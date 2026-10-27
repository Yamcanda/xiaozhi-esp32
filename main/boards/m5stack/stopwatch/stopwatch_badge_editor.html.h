#pragma once

// Deliberately a C++ raw string: no generated assets or CMake embedding is needed.
inline constexpr char kStopwatchBadgeEditorHtml[] = R"BADGEHTML(<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="referrer" content="no-referrer">
<title>Stopwatch badges</title>
<style>
:root{color-scheme:light dark;font:16px/1.45 system-ui,sans-serif;--bg:#f4f5f7;--card:#fff;--ink:#19232d;--muted:#536170;--line:#aeb8c3;--accent:#075a9c}
@media(prefers-color-scheme:dark){:root{--bg:#14191f;--card:#212932;--ink:#edf1f5;--muted:#b1becb;--line:#5d7184;--accent:#8bc9ff}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink)}main{max-width:760px;margin:auto;padding:20px 16px 40px}h1{margin:0 0 8px;font-size:1.65rem}h2{font-size:1.2rem;margin:0 0 12px}p{margin:8px 0;color:var(--muted)}section{margin-top:20px;padding:16px;background:var(--card);border:1px solid var(--line);border-radius:14px}button,input{font:inherit}button{min-height:44px;border:1px solid var(--line);border-radius:9px;background:var(--card);color:var(--ink);padding:8px 14px;cursor:pointer}button:disabled{opacity:.5;cursor:default}button:focus-visible,input:focus-visible{outline:3px solid var(--accent);outline-offset:3px}.primary{background:var(--accent);color:var(--card);font-weight:650}.slots{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:10px}.slot{padding:8px;display:flex;flex-direction:column;align-items:center;gap:5px}.slot[aria-pressed=true]{outline:3px solid var(--accent);outline-offset:1px}.slot img{width:100%;max-width:130px;aspect-ratio:1;border-radius:50%;object-fit:cover;background:var(--bg)}.slot span{font-size:.9rem}.actions{display:flex;flex-wrap:wrap;gap:10px;margin-top:14px}label{display:flex;gap:12px;align-items:center;flex-wrap:wrap;margin:16px 0 8px}input[type=range]{flex:1;min-width:130px}input[type=file]{max-width:100%}input[type=color]{width:64px;height:44px;padding:3px}canvas{display:block;width:min(100%,370px);height:auto;aspect-ratio:1;margin:16px auto;border-radius:50%;outline:1px solid var(--line);touch-action:none;cursor:grab;background:#fff}canvas:active{cursor:grabbing}#message{min-height:1.6em;white-space:pre-wrap;color:var(--ink)}#message.error{color:#b52828}@media(prefers-color-scheme:dark){#message.error{color:#ffb0a8}}.hint{font-size:.9rem}#crop[hidden]{display:none}
</style>
</head>
<body><main>
<h1>Stopwatch badges</h1>
<p>Choose a slot, crop a photo, then save it to the watch. All editing stays in this browser.</p>
<p class="hint">This temporary link grants access to your badges. Use a trusted LAN. Close the editor with Home or Done on the watch when finished.</p>
<p id="message" role="status" aria-live="polite">Connecting…</p>
<section>
<h2>Six badge slots</h2>
<div id="slots" class="slots" aria-label="Badge slots"></div>
<div class="actions"><button id="activate" disabled>Show on watch</button><button id="remove" disabled>Delete selected</button><button id="refresh">Refresh</button></div>
<p id="selection" class="hint"></p>
</section>
<section>
<h2 id="editTitle">Create a badge for slot 1</h2>
<label for="file">Choose a photo <input id="file" type="file" accept="image/jpeg,image/png,image/webp,image/*"></label>
<div id="crop" hidden>
<p class="hint">Drag to position the photo. Zoom to change the crop. The round preview matches the watch.</p>
<canvas id="canvas" width="466" height="466" aria-label="Circular badge crop preview"></canvas>
<label for="zoom">Zoom <input id="zoom" type="range" min="0.25" max="4" step="0.01" value="1"></label>
<label for="background">Background <input id="background" type="color" value="#ffffff"></label>
<div class="actions"><button id="reset">Reset crop</button><button id="save" class="primary">Save to slot 1</button></div>
<p class="hint">Uploads are always a new 466 × 466 JPEG (quality 84%), never the original photo. A successful save also shows that slot on the watch.</p>
</div>
</section>
</main>
<script>
'use strict';
const $ = id => document.getElementById(id);
const token = new URLSearchParams(location.search).get('token') || '';
const validToken = /^[0-9a-f]{32}$/.test(token);
const maxJpeg = 256 * 1024;
const size = 466;
let selected = 0;
let state = {active:-1, slots:Array(6).fill(false), available:false};
let busy = false;
let photo = null;
let sourceUrl = null;
let photoGeneration = 0;
let previewUrls = [];
let offsetX = 0, offsetY = 0;
let dragging = null;
const canvas = $('canvas');
const context = canvas.getContext('2d', {alpha:false});
const slotButtons = [];
const slotImages = [];
const slotLabels = [];
for(let i=0;i<6;i++){
  const button = document.createElement('button');
  button.type = 'button';
  button.className = 'slot';
  const image = document.createElement('img');
  image.alt = 'Empty badge slot ' + (i+1);
  const label = document.createElement('span');
  button.append(image,label);
  button.addEventListener('click',()=>{selected=i;updateControls();});
  $('slots').append(button);
  slotButtons.push(button);slotImages.push(image);slotLabels.push(label);
}
function message(text,error=false){
  $('message').textContent=text;
  $('message').classList.toggle('error',error);
}
function updateControls(){
  slotButtons.forEach((button,i)=>{
    button.disabled=busy;
    button.setAttribute('aria-pressed',String(selected===i));
    slotLabels[i].textContent='Slot '+(i+1)+(state.active===i?' · Active':state.slots[i]?' · Saved':' · Empty');
  });
  $('activate').disabled=busy||!state.slots[selected]||state.active===selected;
  $('remove').disabled=busy||!state.slots[selected];
  $('refresh').disabled=busy;
  $('file').disabled=busy;
  $('save').disabled=busy||!photo||!state.available;
  $('zoom').disabled=busy;
  $('background').disabled=busy;
  $('reset').disabled=busy;
  $('editTitle').textContent='Create a badge for slot '+(selected+1);
  $('save').textContent='Save to slot '+(selected+1);
  $('selection').textContent='Selected slot '+(selected+1)+'. '+(state.slots[selected]?'Saving replaces this slot only.':'This slot is empty.');
}
function endpoint(path,slot){
  const url=new URL(path,location.origin);
  url.searchParams.set('token',token);
  if(slot!==undefined)url.searchParams.set('slot',String(slot));
  return url;
}
async function api(path,{slot,method='GET',body}={}){
  const controller=new AbortController();
  const timer=setTimeout(()=>controller.abort(),40000);
  try{
    const response=await fetch(endpoint(path,slot),{
      method,body,cache:'no-store',credentials:'omit',referrerPolicy:'no-referrer',
      headers:body?{'Content-Type':'image/jpeg'}:{},signal:controller.signal
    });
    if(!response.ok){
      const detail=await response.text();
      throw new Error(detail||('Request failed ('+response.status+').'));
    }
    if(path==='/badge/state')return await response.json();
    if(path==='/badge/image'&&method==='GET')return await response.blob();
    await response.text();
  }catch(error){
    if(error.name==='AbortError')throw new Error('Request timed out. Refresh to check the watch before retrying.');
    if(error instanceof TypeError)throw new Error('Cannot reach the editor. Check the watch is still in editor mode and on the same LAN.');
    throw error;
  }finally{clearTimeout(timer);}
}
async function refreshState(){
  state=await api('/badge/state');
  if(!state.available||!Array.isArray(state.slots)||state.slots.length!==6)throw new Error('Badge storage is unavailable.');
  // Fetch one at a time: the watch has a bounded, single-task HTTP server.
  for(let i=0;i<6;i++){
    if(previewUrls[i]){URL.revokeObjectURL(previewUrls[i]);previewUrls[i]=null;}
    slotImages[i].removeAttribute('src');
    slotImages[i].style.visibility='hidden';
    slotImages[i].alt=state.slots[i]?'Saved badge '+(i+1):'Empty badge slot '+(i+1);
    if(state.slots[i]){
      const blob=await api('/badge/image',{slot:i});
      previewUrls[i]=URL.createObjectURL(blob);
      slotImages[i].src=previewUrls[i];
      slotImages[i].style.visibility='visible';
    }
  }
  updateControls();
}
async function operation(work){
  if(busy)return;
  busy=true;updateControls();
  try{await work();}
  catch(error){message(error.message||'The operation failed.',true);}
  finally{busy=false;updateControls();}
}
$('refresh').addEventListener('click',()=>operation(async()=>{await refreshState();message('Badges refreshed.');}));
$('activate').addEventListener('click',()=>operation(async()=>{
  await api('/badge/active',{method:'POST',slot:selected});
  await refreshState();message('Slot '+(selected+1)+' is now shown on the watch.');
}));
$('remove').addEventListener('click',()=>{
  if(!confirm('Delete badge in slot '+(selected+1)+'?'))return;
  operation(async()=>{
    await api('/badge/image',{method:'DELETE',slot:selected});
    await refreshState();message('Badge deleted.');
  });
});
function render(){
  context.fillStyle=$('background').value;
  context.fillRect(0,0,size,size);
  if(!photo)return;
  const scale=Math.max(size/photo.naturalWidth,size/photo.naturalHeight)*Number($('zoom').value);
  const width=photo.naturalWidth*scale,height=photo.naturalHeight*scale;
  // Permit a chosen background at zoom < 1 while keeping the image within reach.
  offsetX=Math.max(-width/2,Math.min(width/2,offsetX));
  offsetY=Math.max(-height/2,Math.min(height/2,offsetY));
  context.drawImage(photo,(size-width)/2+offsetX,(size-height)/2+offsetY,width,height);
}
function resetCrop(){offsetX=0;offsetY=0;$('zoom').value='1';render();}
$('zoom').addEventListener('input',render);
$('background').addEventListener('input',render);
$('reset').addEventListener('click',resetCrop);
canvas.addEventListener('pointerdown',event=>{
  if(busy||!photo||dragging)return;
  canvas.setPointerCapture(event.pointerId);
  dragging={id:event.pointerId,x:event.clientX,y:event.clientY};
});
canvas.addEventListener('pointermove',event=>{
  if(busy||!dragging||dragging.id!==event.pointerId)return;
  const ratio=size/canvas.getBoundingClientRect().width;
  offsetX+=(event.clientX-dragging.x)*ratio;
  offsetY+=(event.clientY-dragging.y)*ratio;
  dragging.x=event.clientX;dragging.y=event.clientY;render();
});
for(const name of ['pointerup','pointercancel','lostpointercapture']){
  canvas.addEventListener(name,event=>{if(dragging&&dragging.id===event.pointerId)dragging=null;});
}
$('file').addEventListener('change',()=>operation(async()=>{
  const file=$('file').files[0];
  if(!file)return;
  // Browser-side limits avoid accidentally exhausting phone memory on huge files.
  if(file.size>30*1024*1024){message('Choose an image smaller than 30 MiB.',true);return;}
  const generation=++photoGeneration;
  const url=URL.createObjectURL(file);
  const image=new Image();
  try{
    image.src=url;
    await image.decode();
    if(generation!==photoGeneration){URL.revokeObjectURL(url);return;}
    if(!image.naturalWidth||!image.naturalHeight||image.naturalWidth*image.naturalHeight>50000000){
      throw new Error('Choose a smaller image (at most 50 megapixels).');
    }
    if(sourceUrl)URL.revokeObjectURL(sourceUrl);
    sourceUrl=url;photo=image;
    $('crop').hidden=false;resetCrop();updateControls();
    message('Adjust the crop, then save to the selected slot.');
  }catch(error){
    URL.revokeObjectURL(url);
    if(generation===photoGeneration)message(error.message||'This browser could not open the image.',true);
  }
}));
$('save').addEventListener('click',()=>{
  if(state.slots[selected]&&!confirm('Replace badge in slot '+(selected+1)+'?'))return;
  operation(async()=>{
    render();
    message('Creating and validating your JPEG…');
    const blob=await new Promise(resolve=>canvas.toBlob(resolve,'image/jpeg',0.84));
    if(!blob||blob.type!=='image/jpeg')throw new Error('This browser could not export a JPEG.');
    if(blob.size>maxJpeg)throw new Error('The cropped JPEG exceeds 256 KiB. Choose a simpler image or zoom in.');
    message('Saving to the watch…');
    await api('/upload',{method:'POST',slot:selected,body:blob});
    await refreshState();message('Badge saved to slot '+(selected+1)+'.');
  });
});
window.addEventListener('pagehide',()=>{
  previewUrls.forEach(url=>{if(url)URL.revokeObjectURL(url);});
  if(sourceUrl)URL.revokeObjectURL(sourceUrl);
});
updateControls();
if(validToken&&context){
  operation(async()=>{
    await refreshState();
    selected=state.active>=0?state.active:0;
    message('Ready. Choose a slot or a photo.');
  });
}else{message('Invalid editor link or unsupported browser. Reopen the temporary link shown on the watch.',true);}
</script>
</body></html>)BADGEHTML";
