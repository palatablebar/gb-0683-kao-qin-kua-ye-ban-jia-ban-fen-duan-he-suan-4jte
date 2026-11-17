import io,csv,json,hashlib

def bundle(state):
    out=io.StringIO(newline='');writer=csv.writer(out)
    writer.writerow(['记录类型','批次','来源行','业务ID','原因码','内容'])
    rid=state.get('run_id','preview')
    writer.writerow(['运行清单',rid,'','','',json.dumps(state.get('manifest',{}),ensure_ascii=False,sort_keys=True,default=str)])
    writer.writerow(['计价口径',rid,'','','',json.dumps({'basis':state.get('pricing_basis'),'holidays':state.get('holidays',[])},ensure_ascii=False,sort_keys=True,default=str)])
    writer.writerow(['对账',rid,'','','',json.dumps(state.get('reconcile',{}),ensure_ascii=False,sort_keys=True,default=str)])
    for row in state.get('calculations',[]):writer.writerow(['计算',rid,row.get('line',''),row.get('id',''),row.get('code',''),json.dumps(row,ensure_ascii=False,sort_keys=True,default=str)])
    # 补卡原件单独成行，导出凭证能回看补卡前后的取值。
    for row in state.get('corrections_applied',[]):
        writer.writerow(['补卡采纳',rid,'',row.get('employee',''),'',json.dumps(row,ensure_ascii=False,sort_keys=True,default=str)])
    for row in state.get('corrections_rejected',[]):
        writer.writerow(['补卡拒绝',rid,'',(row.get('raw') or {}).get('employee','') if isinstance(row.get('raw'),dict) else '',row.get('code',''),json.dumps(row,ensure_ascii=False,sort_keys=True,default=str)])
    for row in state['quarantine']:writer.writerow(['隔离',rid,row['line'],row.get('id',''),row['code'],json.dumps(row.get('raw',{}),ensure_ascii=False,sort_keys=True,default=str)])
    return '﻿'+out.getvalue()

def checksum(state):
    return hashlib.sha256(bundle(state).encode('utf-8')).hexdigest()
