"""Read-only real-model observation of retained prose; never rewrites saves."""
import copy
import hashlib
import json
import time
import argparse
from pathlib import Path
from open_story_engine.api_read import ReadService
from open_story_engine.api_play import _env_bool, _env_integer, _env_reasoning_effort
from open_story_engine.api_narrative import player_package
from open_story_engine.environment import load_env_file
from open_story_engine.llm import OpenAICompatibleGateway, writer_config_from_env, parse_json_content
from open_story_engine.prompts import render_prompt, catalog_version
from open_story_engine import narrative_delivery as delivery, reader_actions as actions, reader_consequences as consequences
from open_story_engine.entity_facts import turn_entity_facts, observation_state, bind_evidence_paragraphs, bind_player_mentions, bind_new_entity_references

ROOT = Path(__file__).resolve().parents[3]
parser = argparse.ArgumentParser()
parser.add_argument('--run', required=True)
args = parser.parse_args()
OUT = Path(__file__).parent / args.run
OUT.mkdir(exist_ok=False)
load_env_file(ROOT / '.env')
config = writer_config_from_env()
gateway = OpenAICompatibleGateway(config['base_url'], config['api_key'], config['model'],
    _env_bool('STORY_LLM_STREAM', True), _env_integer('STORY_LLM_TIMEOUT_SECONDS', 30, 5, 120),
    _env_integer('STORY_LLM_MAX_TOKENS', 8192, 1024, 8192),
    allow_transport_fallback=_env_bool('STORY_LLM_TRANSPORT_FALLBACK', True),
    reasoning_effort=_env_reasoning_effort(),
    first_delta_timeout_seconds=_env_integer('STORY_LLM_FIRST_DELTA_TIMEOUT_SECONDS', 30, 5, 120))
read = ReadService(ROOT / 'content/packages', ROOT / 'docs/evidence/lethal-action-2026-09-28/sessions.sqlite')
sid = '54ee8e13-77df-5c96-893b-f5d7ab70b491'
results = []
for number in (3, 4):
    source = ROOT / f'docs/evidence/lethal-action-2026-09-28/attempt-09/result-{number:03}.json'
    source_digest = hashlib.sha256(source.read_bytes()).hexdigest()
    node = json.loads(source.read_text())['branch']
    with read.store() as store:
        contract = store.contract(sid)
        lineage = store.lineage(sid, node['parentId'])
        saved = store.branch(sid, node['id'])
    parent = lineage[-1]
    _, package = read.load_package('taixu-relics-part1', '0.1.3')
    player = contract['persona']['sourceCharacterId']
    package = player_package(package, player)
    context = dict(package=package, parent=parent, contract=contract, lineage=lineage, playerDirection=node['playerDirection'])
    state, body = parent['branchState'], node['narrativeText']
    known = actions.registry(package, state)
    facts = turn_entity_facts(package, state, node['playerDirection'], body)
    ids = {e['id'] for e in facts['entities']} | {player, state.get('playerLocationId')}
    payload = dict(input=node['playerDirection'], desiredPlan=None, entityFacts=facts,
        authoritativeState=observation_state(state, ids),
        registry={k:{f:e[f] for f in ('id','name','kind') if f in e} for k,e in known.items() if k in ids},
        goals=state.get('goalLedger', []), threads=parent.get('openThreads', []),
        **delivery.continuity_context(parent,node['playerDirection']), recentContext=[],
        draft={f'P{i+1}':p for i,p in enumerate(body.split('\n\n'))})
    (OUT / f'reobserve-input-{number}.json').write_text(json.dumps(payload,ensure_ascii=False,indent=2)+'\n')
    started=time.monotonic()
    response=gateway.complete_json([{'role':'system','content':render_prompt('reader.state_extract')},
        {'role':'user','content':json.dumps(payload,ensure_ascii=False)}])
    data=parse_json_content(response.content)
    (OUT / f'reobserve-output-{number}.json').write_text(json.dumps(data,ensure_ascii=False,indent=2)+'\n')
    bound=bind_evidence_paragraphs(data,body)
    bound=bind_player_mentions(bound,body,player,known[player]['name'])
    bound=bind_new_entity_references(bound,body,known)
    observed=delivery.observe(context,body,bound)
    projected=consequences.projected_state(state,observed['update'],package)
    after_known=actions.registry(package,projected)
    location=projected.get('playerLocationId')
    with read.store() as store:
        assert store.branch(sid,node['id'])==saved
    assert hashlib.sha256(source.read_bytes()).hexdigest()==source_digest
    result=dict(number=number,mode='read_only_reobservation_not_live_turn',promptVersion=catalog_version(),
        seconds=round(time.monotonic()-started,3),sourceSha256=source_digest,originalNodeUnchanged=True,
        scene=data.get('currentScene'),locationId=location,locationName=after_known.get(location,{}).get('name'),
        locationChanged=location!=state.get('playerLocationId'),diagnostics=observed['diagnostics'])
    results.append(result)
    (OUT / 'reobserve-results.json').write_text(json.dumps(results,ensure_ascii=False,indent=2)+'\n')
    print(json.dumps({k:result[k] for k in ('number','seconds','locationName','locationChanged')},ensure_ascii=False),flush=True)
