import importlib,os
from flask import Flask,jsonify,request,redirect,Response
from app.config import MODULES
from app.errors import DomainError
from app.workflow import run,load,source_of
from app import store
from app.web import payload
from app.modules.m7_archive.export_out import bundle
from app.modules.m5_calculate.rules import BASES

def create_app():
    app=Flask(__name__);app.config['MAX_CONTENT_LENGTH']=2_000_000
    for entry in MODULES:app.register_blueprint(importlib.import_module('app.modules.'+entry[0]+'.handler').bp)
    @app.errorhandler(DomainError)
    def domain_error(error):return jsonify(error.body()),error.status
    @app.errorhandler(404)
    def missing(error):return jsonify(code='NOT_FOUND',message='入口不存在'),404
    @app.get('/')
    def index():return redirect('/'+MODULES[0][0])
    @app.get('/healthz')
    def health():return jsonify(status='ok')
    @app.post('/api/runs')
    def submit():
        content,params=payload();state=run(content,params)
        # 同源输入复用批次：幂等键命中时不覆盖原始批次与补卡原件，只追加修订。
        return jsonify(store.save(state['run_id'],state))
    @app.get('/api/runs')
    def list_runs():return jsonify(store.history())
    @app.get('/api/runs/<run_id>')
    def get_run(run_id):return jsonify(store.get(run_id))
    @app.get('/api/runs/<run_id>/revisions')
    def get_revisions(run_id):return jsonify(revisions=store.revisions(run_id),active=store.active_revision(run_id))
    @app.get('/api/runs/<run_id>/corrections')
    def get_corrections(run_id):return jsonify(store.corrections(run_id))
    @app.post('/api/runs/<run_id>/basis')
    def switch(run_id):
        data=request.get_json(silent=True) or {}
        basis=data.get('basis')
        if basis not in BASES:raise DomainError('PARAMETERS','pricing_basis 只能是 calendar_day 或 shift_day')
        return jsonify(_recalculate(run_id,{'pricing_basis':basis}))
    @app.post('/api/runs/<run_id>/recalculate')
    def recalculate(run_id):
        """补卡、休息、假期或计价口径变更后重新核对。

        候选结果先在内存算完并自检；成功才追加新的修订并设为生效版本，
        失败时旧版结果与既有补卡原件保持不变。
        """
        data=request.get_json(silent=True) or {}
        overrides=data.get('parameters',{})
        if not isinstance(overrides,dict):raise DomainError('PARAMETERS','parameters 必须是对象')
        return jsonify(_recalculate(run_id,overrides))
    @app.post('/api/runs/<run_id>/review')
    def review(run_id):
        data=request.get_json(silent=True) or {}
        if not isinstance(data.get('version'),int):raise DomainError('VERSION','请提交复核版本')
        return jsonify(store.review(run_id,data['version'],data.get('decision')))
    @app.get('/api/runs/<run_id>/reviews')
    def reviews(run_id):return jsonify(store.review_history(run_id))
    @app.get('/api/archive/<run_id>/export.csv')
    def download(run_id):
        current=store.get(run_id)
        revision=request.args.get('revision',type=int)
        state=current;fallback=False
        if revision is not None:
            try:
                candidate=store.revision_body(run_id,revision)
                if candidate.get('reconcile',{}).get('balanced'):
                    state=candidate
                else:
                    fallback=True
            except DomainError:
                # 指定的修订已不可导出时回到当前生效版本，不让页面/导出的证据切换失败。
                fallback=True
        elif not current.get('reconcile',{}).get('balanced'):
            raise DomainError('UNBALANCED','对账未平')
        headers={'Content-Disposition':'attachment; filename=evidence.csv',
                 'X-Archive-Revision':str(state.get('active_revision','')),
                 'X-Archive-Fallback':'active' if fallback else 'none'}
        return Response(bundle(state),mimetype='text/csv; charset=utf-8',headers=headers)
    with app.app_context():
        if not store.history():
            state=run(load('dirty.csv'),{});store.save(state['run_id'],state)
    return app

def _recalculate(run_id,overrides):
    """以已存批次的原始输入与参数为基线，叠加变更后重算。

    失败恢复旧版：候选结果只在内存中构建；对账不平、规则拒绝或任何异常
    都不会写入新修订，生效版本保持原样。
    """
    current=store.get(run_id)
    previous=store.active_revision(run_id)
    params=dict((current.get('manifest') or {}).get('parameters') or {})
    params.update(overrides)
    candidate=run(source_of(current),params)
    # 批次 ID 与归档指纹都绑定在原批次上，重算不能改写。
    from app.modules.m7_archive.export_out import checksum
    candidate['run_id']=run_id
    candidate['archive']['sha256']=checksum(candidate)
    if not candidate['reconcile']['balanced']:
        raise DomainError('UNBALANCED','重算对账未平，已保留旧版结果',409,
                          {'kept_revision':previous,'active_revision':previous})
    if not candidate['archive']['ready']:
        raise DomainError('ARCHIVE','封存校验未通过，已保留旧版结果',409,
                          {'kept_revision':previous,'active_revision':previous})
    # 结论完全相同的重算不制造新修订，也不打断已完成的复核。
    if previous and store.active_signature(run_id)==store.signature(candidate):
        result=store.get(run_id)
        result['switched_from']=previous;result['active_revision']=previous;result['changed']=False
        return result
    revision=store.commit_revision(run_id,candidate)
    result=store.get(run_id)
    result['switched_from']=previous;result['active_revision']=revision;result['changed']=True
    return result

app=create_app()
if __name__=='__main__':app.run(host='0.0.0.0',port=8000)
