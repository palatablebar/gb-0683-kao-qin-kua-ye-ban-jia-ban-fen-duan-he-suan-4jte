import base64
from flask import request,render_template
from app.errors import DomainError
from app.config import TITLE,MODULES,SHORT
from app.workflow import run,load
from app import store

def payload():
    data=request.get_json(silent=True) or {}
    if not isinstance(data,dict):raise DomainError('PAYLOAD','请求必须是对象')
    if 'csv' in data and not isinstance(data['csv'],str):raise DomainError('CSV','CSV内容必须是文本')
    sample=data.get('sample',request.args.get('sample','dirty.csv'))
    content=data.get('csv')
    return (content.encode() if isinstance(content,str) else load(sample)),data.get('parameters',{})

def api(stage):
    content,parameters=payload();return run(content,parameters,stage)

def page(stage,template):
    latest=store.history()
    if not latest:
        state=run(load('dirty.csv'),{});latest=[]
    else:
        # 页面读取当前生效修订，与导出使用同一份业务结论。
        state=store.get(latest[0]['id'])
        latest=store.history()
    if 'sample' in request.args:
        data=run(load(request.args['sample']),{},stage)
    elif stage>=7 and state.get('archive'):
        # 归档页直接使用生效修订，保证页面封存指纹与导出证据一致。
        data=dict(state);data['run_id']=state['run_id'];data['active_revision']=state.get('active_revision')
    else:
        source=base64.b64decode(state['source_b64']) if state.get('source_b64') else load('dirty.csv')
        params=state.get('manifest',{}).get('parameters',{}) or {}
        data=run(source,params,stage)
        # 页面展示的结果挂在原批次上，补卡与口径切换不改变批次 ID。
        data['run_id']=state['run_id']
        data['active_revision']=state.get('active_revision')
    revisions=store.revisions(state['run_id']) if latest else []
    corrections=store.corrections(state['run_id']) if latest else []
    return render_template(template,title=TITLE,short=SHORT,modules=MODULES,current=stage,data=data,run=state,history=latest,revisions=revisions,corrections=corrections)
