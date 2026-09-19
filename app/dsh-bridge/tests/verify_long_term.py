"""Live provider + real local embeddings, isolated from the user's conversations."""
import json
import os
from pathlib import Path
import shutil
import sys
import time
import uuid

APP = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(APP / 'dsh-bridge'), str(APP)]
root = APP / 'output/memory-verification' / ('long-term-' + uuid.uuid4().hex[:10])
shutil.copytree(APP / 'dsh-bridge/dsh-home/profiles', root / 'home/profiles')
(root / 'workspace').mkdir()
os.environ['DSH_SESSION_PATH'] = str(root / 'session.json')
os.environ['DSH_PERSONA_PATH'] = str(root / 'persona.yml')

import server
import ui.server as ui
from starlette.requests import Request
from memory import read_json, write_json

server._cfg = server.load_config()
server._cfg.update(dsh_home=str(root / 'home'), cwd=str(root / 'workspace'))
config = read_json(APP / 'heygem-data/llm-provider.json')
provider = next(p for p in config['providers'] if p['id'] == config['active'])
request = Request({'type': 'http', 'headers': [
    (b'authorization', ('Bearer ' + provider['api_key']).encode()),
    (b'x-upstream-base-url', provider['base_url'].encode()),
]})
checks = []


def chat(text):
    messages = [{'role': 'system', 'content': '你是中文陪伴助手。只回答current_user，背景仅供参考；不调用工具。'},
                {'role': 'user', 'content': text},
                {'role': 'user', 'content': 'Please reply to my message in Chinese.'}]
    payload = ui.prepare_llm_payload({'messages': messages}, provider)
    start = time.monotonic()
    response = server.chat_completions(server.ChatRequest(**payload), request)
    return response['choices'][0]['message']['content'], round(time.monotonic() - start, 2)


def passed(name, **extra):
    item = {'check': name, 'pass': True, **extra}
    checks.append(item); print(json.dumps(item, ensure_ascii=False), flush=True)


try:
    # Warm CPU model before latency checks.
    store = server.long_term_store()
    store.embeddings.encode(['开始'])
    ui.mark_affinity_opening()
    answer, seconds = chat('我叫林沐，请记住。现在只回复“第一轮收到”。')
    assert '第一轮收到' in answer, answer
    passed('first_turn_answers_actual_message', seconds=seconds)
    answer, seconds = chat('现在只回复“第二轮收到”。')
    assert '第二轮收到' in answer, answer
    passed('second_turn_not_shifted', seconds=seconds)
    server._memory_jobs.submit(lambda: None).result(timeout=120)
    assert any('林沐' in f['value'] for f in store.facts()), store.facts()
    passed('automatic_fact_extraction_has_source')
    server.close_harness()
    answer, seconds = chat('我叫什么名字？')
    assert '林沐' in answer, answer
    passed('name_recalled_after_runtime_restart', seconds=seconds)
    server._memory_jobs.submit(lambda: None).result(timeout=120)

    source = store.record('synthetic-old-history', '昨晚我和朋友吵架了，回家后哭了好久。', '听起来你很委屈。')
    store.record('other', '我周末去游泳。', '注意安全。')
    store.record('other', '我今天买了一台咖啡机。', '享受咖啡。')
    while store.index_pending(): pass
    hits = store.search('我上次为什么心情不好？')
    assert hits and hits[0]['id'] == source, hits
    passed('semantic_paraphrase_finds_old_event', score=hits[0]['score'])

    # New DSH lineage with no old transcript: answer must use separately stored facts/history.
    server.close_harness()
    write_json(server.SESSION_PATH, {'history': []})
    answer, seconds = chat('我叫什么名字？我上次为什么心情不好？')
    assert '林沐' in answer and ('吵架' in answer or '争吵' in answer), answer
    passed('facts_and_semantic_history_injected_into_empty_context', seconds=seconds)
    server._memory_jobs.submit(lambda: None).result(timeout=120)

    store.save_fact('姓名', '林澈', manual=True, quote='用户在记忆面板确认')
    answer, _ = chat('我的名字是什么？')
    assert '林澈' in answer, answer
    passed('manual_correction_overrides_old_conversation')
    server._memory_jobs.submit(lambda: None).result(timeout=120)
    write_json(root / 'results.json', {'checks': checks})
    print('RESULTS', root / 'results.json', flush=True)
finally:
    server.close_harness()
    server._memory_jobs.shutdown(wait=True)
