import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

APP = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(APP / 'dsh-bridge'), str(APP)]
import server
import ui.server as ui


class OutputModeTests(unittest.TestCase):
    def test_mode_replaces_fixed_length_policy(self):
        original = ('人格正文\n【说话长度 · 硬性要求】\n每次回复不超过 96 个字。\n'
                    '【输出格式】\n只说话。\n' + server._VOICE_BRIEF_RULE)
        voice = server.persona_for_output_mode(original, 'voice-only')
        digital = server.persona_for_output_mode(original, 'digital-human')
        self.assertNotIn('每次回复不超过 96 个字', voice)
        self.assertNotIn(server._VOICE_BRIEF_RULE, voice)
        self.assertIn('一百二十到四百八十', voice)
        self.assertIn('最多九十六', digital)
        self.assertIn('【输出格式】', digital)

    def test_digital_human_has_hard_96_character_guard(self):
        long = '这是第一句。' + '很长的内容，' * 30
        clipped = server.spoken_text_for_mode(long, 'digital-human')
        self.assertLessEqual(len(clipped), 96)
        self.assertEqual(server.spoken_text_for_mode(long, 'voice-only'), long)

    def test_voice_sse_groups_sentences_near_90_characters(self):
        text = ('第一句话用来说明今天发生的事情，也交代一下前因后果。'
                '第二句话继续补充当时的感受和一些必要细节。'
                '第三句话换个角度，说说后来我是怎么想明白的。'
                '第四句话把剩下的内容说完，让前一部分自然结束。'
                '第五句话再补充一个不同的观察，避免内容太单薄。'
                '第六句话负责收尾，让整段表达听起来更加完整。')
        frames = list(server._sse_chunks(text, 'test'))
        contents = []
        for frame in frames:
            if not frame.startswith('data: {'):
                continue
            delta = json.loads(frame[6:])['choices'][0]['delta']
            if delta.get('content'):
                contents.append(delta['content'])
        self.assertEqual(''.join(contents), text)
        self.assertGreaterEqual(len(contents), 2)
        self.assertTrue(all(part[-1] in '。！？；!?;\n' for part in contents))
        self.assertTrue(all(len(part) <= 120 for part in contents))

    def test_ui_sends_server_side_output_mode(self):
        provider = {'api_key': 'test', 'base_url': 'https://example.invalid/v1', 'label': 'test'}
        with patch.object(ui, 'lipsync_enabled', return_value=True):
            self.assertEqual(ui.llm_completion_target(provider)[1]['X-Companion-Output-Mode'], 'digital-human')
        with patch.object(ui, 'lipsync_enabled', return_value=False):
            self.assertEqual(ui.llm_completion_target(provider)[1]['X-Companion-Output-Mode'], 'voice-only')


if __name__ == '__main__':
    unittest.main()
