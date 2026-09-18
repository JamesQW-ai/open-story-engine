import json,os,sys,time
from pathlib import Path
sys.path.insert(0,'/Users/James/open-story-engine')
from open_story_engine.api_play import PlayService
from open_story_engine.api_read import ReadService
from open_story_engine.api_reader_quality import action_requirements
from open_story_engine.reader_scene_review import grounding_claims, public_scene_evidence, combined_scene_issues
from open_story_engine.prompts import render_prompt,catalog_version
from open_story_engine.llm import parse_json_content
root=Path('/Users/James/open-story-engine');out=root/'docs/evidence/p0-p1-scope-2026-09-16'
os.environ['STORY_PLANNER']='openai'
read=ReadService(root/'content/packages',root/'data/open-story-engine.sqlite');play=PlayService(read,root);gw=play._planner.gateway;gw.remaining_calls=2
with read.store() as store:
 contract=store.contract('22b9da04-7a01-5489-a7c4-682e88fab80d');lineage=store.lineage('22b9da04-7a01-5489-a7c4-682e88fab80d','branch_f85ea073-75e9-42c9-894c-d1f17bbed4de')
results=[]
for batch,name in [(3,'ask'),(0,'control')]:
 source=root/f'docs/evidence/p0-p1-stability-2026-09-16/batch-{batch or 3}'
 sample=json.loads((source/f'{name if batch else "ask"}.json').read_text());body=sample['result']['branch']['narrativeText'];history=lineage[:]
 if name=='ask':history.append(json.loads((source/'inform.json').read_text())['result']['branch'])
 if not batch:body='你留在原地，让陆照临只看手里的纸包，没有交出去。\n\n“木牌刻痕里发现了金屑。你怎么看，有什么依据？”你问。\n\n陆照临只看纸包，没有转看木牌。“我不知道它的来处，也没有判断来处的依据。”他说。你听完，仍未决定下一步。'
 req=action_requirements(sample['input']['text']);evidence=public_scene_evidence({'contract':contract,'lineage':history})
 payload={'paragraphs':grounding_claims(body),'draft':{f'P{i+1}':p for i,p in enumerate(body.split('\n\n'))},'input':sample['input']['text'],'requirements':req,'sceneEvidence':evidence}
 started=time.monotonic();completion=gw.complete_json([{'role':'system','content':render_prompt('reader.scene_grounding')},{'role':'user','content':json.dumps(payload,ensure_ascii=False)}]);review=parse_json_content(completion.content)
 try:combined_scene_issues({'issues':[]},body,[],review,evidence=evidence,requirements=req);status='accepted';error=None
 except ValueError as exc:status='rejected';error=str(exc)
 record={'case':f'batch-{batch}-{name}','promptVersion':catalog_version(),'status':status,'elapsedSeconds':round(time.monotonic()-started,2),'error':error,'input':payload,'review':review,'rawResponse':completion.raw_response,'observations':completion.observations};results.append(record)
 (out/'probes-v2.json').write_text(json.dumps(results,ensure_ascii=False,indent=2));print(json.dumps({k:record[k] for k in ['case','status','elapsedSeconds','error']},ensure_ascii=False),flush=True)
play.drafts.close()
