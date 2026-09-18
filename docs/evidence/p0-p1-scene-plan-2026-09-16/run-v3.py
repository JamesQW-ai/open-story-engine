import hashlib,json,os,re,sqlite3,sys,tempfile,time
from pathlib import Path
sys.path.insert(0,'/Users/James/open-story-engine')
from open_story_engine.api_play import PlayService
from open_story_engine.api_read import ReadService
from open_story_engine.prompts import catalog_version
root=Path('/Users/James/open-story-engine')
folder=Path(tempfile.mkdtemp(prefix='ose-scene-plan-'))
Path('/private/tmp/ose-scene-plan-current').write_text(str(folder))
source=root/'data/open-story-engine.sqlite'; target=folder/'sessions.sqlite'
before=hashlib.sha256(source.read_bytes()).hexdigest()
with sqlite3.connect(f'file:{source}?mode=ro',uri=True) as src, sqlite3.connect(target) as dst: src.backup(dst)
os.environ['STORY_PLANNER']='openai'
read=ReadService(root/'content/packages',target); play=PlayService(read,root)
assert play.generation_available
play._planner.gateway.remaining_calls=18
sid='22b9da04-7a01-5489-a7c4-682e88fab80d'; parent='branch_f85ea073-75e9-42c9-894c-d1f17bbed4de'
actions={k:json.loads((root/f'docs/evidence/d2-repair-stability-2026-09-16/{v}.json').read_text())['input']['text'] for k,v in [('inform','intent-7'),('ask','explain-7'),('wait','brief-7')]}
report={'promptVersion':catalog_version(),'mainDatabaseBefore':before,'perTurnMaxTransportCalls':18,'turns':[]}
for kind,action in actions.items():
    request={'parent_branch_id':parent,'text':action,'request_id':'scene-plan-v3-'+kind}
    started=time.monotonic(); chunks=[]; first=[]
    def delta(text):
        if not first: first.append(round((time.monotonic()-started)*1000))
        chunks.append(text)
    try:
        result=play.continue_turn(sid,**request,stream=delta,stream_reset=lambda _:chunks.clear())
    except Exception as error:
        result={'status':'failed','error':str(error),'code':getattr(error,'code',None)}
    record={'input':request,'elapsedSeconds':round(time.monotonic()-started,2),'firstTextMs':first[0] if first else None,'result':result,'visibleTextOnFailure':''.join(chunks) if result['status']=='failed' else None}
    (folder/(kind+'.json')).write_text(json.dumps(record,ensure_ascii=False,indent=2))
    job=next(j for j in play.drafts.jobs.values() if j['binding'].get('request_id')==request['request_id'])
    (folder/(kind+'-job.json')).write_text(json.dumps({k:v for k,v in job.items() if k not in ('snapshot','events','subscribers')},ensure_ascii=False,indent=2))
    body=result.get('branch',{}).get('narrativeText','')
    summary={'kind':kind,'status':result['status'],'elapsedSeconds':record['elapsedSeconds'],'firstTextMs':record['firstTextMs'],'actualCjk':len(re.findall(r'[\u3400-\u4dbf\u4e00-\u9fff]',body)),'metrics':job['metrics']}
    report['turns'].append(summary)
    if result.get('branch'): parent=result['branch']['id']
    (folder/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2))
    print(json.dumps(summary,ensure_ascii=False),flush=True)
play.drafts.close()
report['mainDatabaseAfter']=hashlib.sha256(source.read_bytes()).hexdigest()
report['databaseUnchanged']=report['mainDatabaseAfter']==before
(folder/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2))
print(str(folder),flush=True)
