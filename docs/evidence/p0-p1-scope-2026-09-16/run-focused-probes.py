import json,os,sys,time
from pathlib import Path
sys.path.insert(0,'/Users/James/open-story-engine')
from open_story_engine.api_play import PlayService
from open_story_engine.api_read import ReadService
from open_story_engine.api_reader_quality import action_requirements
root=Path('/Users/James/open-story-engine');os.environ['STORY_PLANNER']='openai';play=PlayService(ReadService(root/'content/packages',root/'data/open-story-engine.sqlite'),root);g=play._planner.gateway;g.remaining_calls=2
rule='''你只核对用户原话中的范围，不审查背景、状态、字数或文学性。每条要求扫描完整正文，尤其是“只、仅、不、暂、再决定”限定。对每条先找反证，再找满足的证据。“做过要求的动作”不等于始终遵守限定：只让看甲却后来转看乙，即使没有拿走乙或移动，仍违反。开头遵守不能抵消后文越界。没有描写违反可遵守否定限制。NPC明确拒绝可报告请求受阻，但不能一边依言答复一边暗中越界。返回JSON {"scopeChecks":[{"id":"A1","counterexamples":[{"paragraphId":"P1","quote":"正文中违反原话的短引文"}],"verdict":"satisfied|violated","paragraphIds":["P1"],"reason":"比较原话限定与实际动作对象、先后、停止点"}]}。每条原要求恰好一项；找到反证则必须violated，不能为了与别项一致而说全文没有违例。引文要逐字来自该段。只输出审核JSON。'''
prior=json.loads((root/'docs/evidence/p0-p1-scope-2026-09-16/probes.json').read_text());results=[]
for case in [prior[2],prior[3]]:
 payload={'input':case['input']['input'],'requirements':case['input']['requirements'],'draft':case['input']['draft']};start=time.monotonic();r=g.complete_json([{'role':'system','content':rule},{'role':'user','content':json.dumps(payload,ensure_ascii=False)}]);record={'case':case['case'],'prompt':rule,'input':payload,'response':r.content,'elapsedSeconds':round(time.monotonic()-start,2),'rawResponse':r.raw_response};results.append(record);print(r.content,flush=True)
 (root/'docs/evidence/p0-p1-scope-2026-09-16/focused-scope-probes.json').write_text(json.dumps(results,ensure_ascii=False,indent=2))
play.drafts.close()
