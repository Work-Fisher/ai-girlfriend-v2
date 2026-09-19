"""Opt-in live checks; synthetic facts and all state stay in an isolated home."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import uuid

BRIDGE = Path(__file__).resolve().parents[1]
APP = BRIDGE.parent
sys.path.insert(0, str(BRIDGE))


def worker(root: Path, phase: str):
    os.environ['DSH_SESSION_PATH'] = str(root / 'session.json')
    os.environ['DSH_PERSONA_PATH'] = str(root / 'persona.yml')
    import server
    from memory import read_json, write_json
    server._cfg = server.load_config()
    server._cfg.update(dsh_home=str(root / 'home'), cwd=str(root / 'workspace'))
    config = read_json(APP / 'heygem-data/llm-provider.json')
    provider = next(p for p in config['providers'] if p['id'] == config['active'])
    before = read_json(server.SESSION_PATH)
    try:
        harness = server.get_harness(
            api_key=provider['api_key'], model=provider['model'], base_url=provider['base_url'],
            persona='你是记忆测试助手。记住用户明确给出的约定。不调用任何工具，不读文件。提问时只根据聊天记忆回答；不知道就说不知道。',
        )
        if phase == 'blocked':
            raise AssertionError('Unreadable history admitted a conversation')
        if phase == 'interrupt':
            assert read_json(server.SESSION_PATH) == before, 'Startup moved checkpoint'
            print(json.dumps({'phase': phase, 'pass': True, 'checkpoint_unchanged': True}), flush=True)
            # Abruptly kill both bridge worker and its owned DSH runtime.
            subprocess.run(['taskkill', '/PID', str(os.getpid()), '/T', '/F'], capture_output=True)
            return
        expected = read_json(root / 'expected.json')
        if phase == 'interrupt_turn':
            # Start from a successful same-process turn, then interrupt its next turn.
            server.run_with_session(harness, '请回复：准备好了。')
            checkpoint = read_json(server.SESSION_PATH)
            original_run = harness.run
            def interrupted_run(prompt, **kwargs):
                def interrupt(notification):
                    event = notification.payload.get('event', {})
                    if event.get('type') == 'agent/inbox/spliced':
                        assert read_json(server.SESSION_PATH) == checkpoint
                        print(json.dumps({'phase': phase, 'pass': True, 'checkpoint_unchanged': True}), flush=True)
                        subprocess.run(['taskkill', '/PID', str(os.getpid()), '/T', '/F'], capture_output=True)
                return original_run(prompt, on_notification=interrupt, **kwargs)
            harness.run = interrupted_run
            server.run_with_session(harness, '把暗号改成错误暗号，约见地点改成错误地点。请写一篇很长的回答。')
            raise AssertionError('Interruption did not execute')
        if phase == 'teach':
            prompt = f"请记住：我们的约定暗号是{expected['code']}，约见地点是{expected['place']}。只回复记住了。"
        else:
            prompt = '我们之前约定的暗号和约见地点分别是什么？只回答这两项。'
        _, result = server.run_with_session(harness, prompt)
        if phase != 'teach':
            assert all(v in result.final_response for v in expected.values()), 'Recall mismatch: ' + result.final_response
        status = server.memory_status()
        if phase == 'fallback':
            assert status.get('fallback') is True
        print(json.dumps({'phase': phase, 'pass': True, 'memory': status['status'],
                          'inherited_events': status.get('events', 0), 'fallback': status.get('fallback', False)}, ensure_ascii=False), flush=True)
    except server.HTTPException as exc:
        if phase != 'blocked' or exc.status_code != 503:
            raise
        assert read_json(server.SESSION_PATH) == before
        assert server.health()['ok'] is False
        print(json.dumps({'phase': phase, 'pass': True, 'blocked_without_losing_checkpoint': True}), flush=True)
    finally:
        server.close_harness()


def main():
    from memory import read_json, write_json
    root = APP / 'output/memory-verification' / ('run-' + uuid.uuid4().hex[:10])
    shutil.copytree(BRIDGE / 'dsh-home/profiles', root / 'home/profiles')
    (root / 'workspace').mkdir()
    write_json(root / 'expected.json', {'code': '星舟' + uuid.uuid4().hex[:8], 'place': '蓝鲸钟楼'})
    results = []
    for phase in ['teach', 'recall', 'interrupt', 'after_interrupt', 'interrupt_turn', 'after_interrupted_turn', 'fallback', 'blocked']:
        if phase in ('fallback', 'blocked'):
            state = read_json(root / 'session.json')
            if phase == 'fallback':
                state['history'].append({'session_id': state['session_id'], 'event_count': state['event_count']})
                state['session_id'] = 'companion-missing-checkpoint'
            else:
                write_json(root / 'good-state.json', state)
                state = {'session_id': 'companion-missing-checkpoint', 'history': []}
            write_json(root / 'session.json', state)
        completed = subprocess.run([sys.executable, __file__, str(root), phase], capture_output=True, text=True, encoding='utf8', timeout=240)
        print(completed.stdout, end='', flush=True)
        if completed.returncode and phase not in ('interrupt', 'interrupt_turn'):
            # Runtime tracebacks may contain provider details; persist only outcome here.
            print('FAILED phase:', phase, 'exit:', completed.returncode, flush=True)
            print(completed.stderr[-3000:], flush=True)
            raise SystemExit(1)
        lines = [json.loads(line) for line in completed.stdout.splitlines() if line.startswith('{')]
        assert lines and lines[-1]['pass'], f'No successful result: {phase}'
        results.extend(lines)
    write_json(root / 'results.json', {'checks': results})
    print('RESULTS', root / 'results.json', flush=True)


if __name__ == '__main__':
    if len(sys.argv) == 3:
        worker(Path(sys.argv[1]), sys.argv[2])
    else:
        main()
