'use strict';
const sheet={pending:false};

function status(text){
  const message=document.getElementById('message');
  if(message)message.textContent=text;
}

async function recount(input){
  if(sheet.pending)return;
  sheet.pending=true;status('工时重算中…');
  try{
    const response=await fetch('/api/runs',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(input)});
    const data=await response.json();
    if(!response.ok)throw new Error(data.message);
    location.reload();
  }catch(error){
    status(error.message+'，已保留上一版结果');
  }finally{sheet.pending=false;}
}

async function switchBasis(button){
  if(sheet.pending)return;
  const main=document.querySelector('main');
  // 切换前留存整块工作表；失败时原样恢复，并重新绑定事件与状态栏。
  const previous=main.innerHTML;
  sheet.pending=true;button.disabled=true;status('正在切换为「'+button.textContent.trim()+'」…');
  try{
    const response=await fetch('/api/runs/'+button.dataset.id+'/basis',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({basis:button.dataset.basis})});
    const data=await response.json();
    if(!response.ok)throw new Error(data.message||'切换失败');
    location.reload();
  }catch(error){
    main.innerHTML=previous;
    bind();
    status(error.message+'，已恢复旧版结果');
  }finally{sheet.pending=false;}
}

function bind(){
  const file=document.getElementById('clock-file');
  if(file)file.addEventListener('submit',event=>{
    event.preventDefault();
    const values=new FormData(file);const csv=values.get('csv');
    recount(csv?{csv}:{sample:values.get('sample')});
  });
  const check=document.getElementById('check');
  if(check)check.addEventListener('click',async()=>{
    try{
      const response=await fetch('/api/runs/'+check.dataset.id+'/review',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({version:Number(check.dataset.version),decision:'通过'})});
      const data=await response.json();
      if(!response.ok)throw Error(data.message);
      check.dataset.version=data.review_version;status('工资复核通过 / '+data.review_version);
    }catch(error){status(error.message);}
  });
  const paper=document.getElementById('paper');
  if(paper)paper.addEventListener('click',()=>window.print());
  document.querySelectorAll('[data-basis]').forEach(button=>button.addEventListener('click',()=>switchBasis(button)));
  document.querySelectorAll('td[tabindex]').forEach(cell=>cell.addEventListener('focus',()=>status(cell.textContent.trim())));
}

const recountButton=document.getElementById('recount');
if(recountButton)recountButton.addEventListener('click',()=>recount({sample:'dirty.csv'}));
bind();
