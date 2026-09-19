"""Live, isolated verification of digital-human and voice-only reply policies."""
import json
import os
from pathlib import Path
import shutil
import sys
import uuid

APP = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(APP / 'dsh-bridge'), str(APP)]
root = APP / 'output/memory-verification' / ('output-modes-' + uuid.uuid4().hex[:10])
shutil.copytree(APP / 'dsh-bridge/dsh-home/profiles', root / 'home/profiles')
(root / 'workspace').mkdir()
os.environ['DSH_SESSION_PATH'] = str(root / 'session.json')
os.environ['DSH_PERSONA_PATH'] = str(root / 'persona.yml')

import server
from memory import read_json, write_json

server._cfg = server.load_config()
server._cfg.update(dsh_home=str(root / 'home'), cwd=str(root / 'workspace'))
config = read_json(APP / 'heygem-data/llm-provider.json')
provider = next(p for p in config['providers'] if p['id'] == config['active'])
base_persona = '你是自然的中文陪伴者。回答当前问题，不调用工具。'
checks = []


def run(mode, prompt):
    persona = server.persona_for_output_mode(base_persona, mode)
    harness = server.get_harness(api_key=provider['api_key'], model=provider['model'],
                                 base_url=provider['base_url'], persona=persona)
    envelope = json.dumps({'instruction': '只回答current_user。', 'current_user': prompt}, ensure_ascii=False)
    _, result = server.run_with_session(harness, envelope)
    return server.spoken_text_for_mode(result.final_response, mode)


try:
    digital = run('digital-human', '请详细解释为什么人与人之间需要认真倾听，给出多个角度。')
    assert 0 < len(digital) <= 96, len(digital)
    checks.append({'check': 'digital_human_max_96', 'pass': True, 'characters': len(digital)})
    print(json.dumps(checks[-1], ensure_ascii=False), flush=True)

    voice = run('voice-only', '请用大约三百个中文字符解释为什么人与人之间需要认真倾听，要自然口语化，分成多句。')
    chunks = []
    for frame in server._sse_chunks(voice, provider['model']):
        if frame.startswith('data: {'):
            delta = json.loads(frame[6:])['choices'][0]['delta']
            if delta.get('content'):
                chunks.append(delta['content'])
    assert len(voice) > 96, len(voice)
    assert len(chunks) >= 3, chunks
    assert ''.join(chunks) == voice
    checks.append({'check': 'voice_long_and_sentence_segmented', 'pass': True,
                   'characters': len(voice), 'segments': len(chunks)})
    print(json.dumps(checks[-1], ensure_ascii=False), flush=True)
    write_json(root / 'results.json', {'checks': checks})
    print('RESULTS', root / 'results.json', flush=True)
finally:
    server.close_harness()
    server._memory_jobs.shutdown(wait=True)
