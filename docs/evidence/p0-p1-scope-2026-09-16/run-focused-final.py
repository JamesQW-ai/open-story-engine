import json,os,sys,time
from pathlib import Path
sys.path.insert(0,'/Users/James/open-story-engine')
from open_story_engine.api_play import PlayService
from open_story_engine.api_read import ReadService
from open_story_engine.api_reader_quality import action_requirements
root=Path('/Users/James/open-story-engine');os.environ['STORY_PLANNER']='openai';play=PlayService(ReadService(root/'content/packages',root/'data/open-story-engine.sqlite'),root);g=play._planner.gateway;g.remaining_calls=3
rule=(root/'open_story_engine/prompts/reader/scope_review.md').read_text()
prior=json.loads((root/'docs/evidence/p0-p1-scope-2026-09-16/probes.json').read_text());results=[]
face=json.loads(json.dumps(prior[3]));face['case']='face-glance-control';face['input']['draft']['P3']='陆照临只看纸包，然后抬眼看着你。“我不知道它的来处，也没有判断来处的依据。”他说。你听完，仍未决定下一步。'
for case in [prior[2],prior[3],face]:
 payload={'input':case['input']['input'],'requirements':case['input']['requirements'],'draft':case['input']['draft']};start=time.monotonic();r=g.complete_json([{'role':'system','content':rule},{'role':'user','content':json.dumps(payload,ensure_ascii=False)}]);record={'case':case['case'],'prompt':rule,'input':payload,'response':r.content,'elapsedSeconds':round(time.monotonic()-start,2),'rawResponse':r.raw_response};results.append(record);print(r.content,flush=True)
 (root/'docs/evidence/p0-p1-scope-2026-09-16/focused-scope-final.json').write_text(json.dumps(results,ensure_ascii=False,indent=2))
play.drafts.close()
