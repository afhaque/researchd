"""LLM clients: LM Studio (OpenAI-compatible) and a mock for dry runs.

Design rule from red-teaming: every call is bounded (timeout + max_tokens),
JSON is parsed defensively (fence-strip, one retry with the error, then a
caller-supplied fallback) so one bad generation never kills the night.
"""

import json
import re

import requests

from .adapters import MOCK_EVIDENCE


class LLMError(Exception):
    pass


def _strip_fences(text: str) -> str:
    text = text.strip()
    match = re.search(r'```(?:json)?\s*(.*?)```', text, re.DOTALL)
    if match:
        return match.group(1).strip()
    # Grab the outermost JSON object if there's prose around it
    start, end = text.find('{'), text.rfind('}')
    if start != -1 and end > start:
        return text[start:end + 1]
    return text


class LMStudioClient:
    def __init__(self, cfg: dict):
        self.base_url = cfg['base_url'].rstrip('/')
        self.model = cfg.get('model') or ''
        self.max_tokens = cfg.get('max_tokens', 2048)
        self.timeout = cfg.get('timeout_seconds', 180)
        self.temperature = cfg.get('temperature', 0.3)

    def preflight(self) -> str:
        """Verify the server is up and a model is loaded. Returns model id."""
        resp = requests.get(f'{self.base_url}/models', timeout=30)
        resp.raise_for_status()
        models = [m['id'] for m in resp.json().get('data', [])]
        if not models:
            raise LLMError('LM Studio is running but no model is loaded')
        if self.model and self.model not in models:
            raise LLMError(f'model {self.model!r} not loaded; available: {models}')
        model = self.model or models[0]
        self.model = model
        self.text('Reply with the single word: ok', step='preflight', max_tokens=8)
        return model

    def text(self, prompt: str, step: str = '', max_tokens: int | None = None) -> str:
        payload = {
            'model': self.model,
            'messages': [{'role': 'user', 'content': prompt}],
            'max_tokens': max_tokens or self.max_tokens,
            'temperature': self.temperature,
        }
        resp = requests.post(f'{self.base_url}/chat/completions',
                             json=payload, timeout=self.timeout)
        resp.raise_for_status()
        return resp.json()['choices'][0]['message']['content']

    def json(self, prompt: str, step: str = '', fallback: dict | None = None) -> dict:
        """Ask for JSON; retry once with the parse error; then fall back."""
        raw = self.text(prompt, step=step)
        for attempt in range(2):
            try:
                return json.loads(_strip_fences(raw))
            except (json.JSONDecodeError, ValueError) as e:
                if attempt == 1:
                    break
                raw = self.text(
                    f'{prompt}\n\nYour previous reply was not valid JSON '
                    f'({e}). Reply with ONLY the JSON object, nothing else.',
                    step=step,
                )
        if fallback is not None:
            return fallback
        raise LLMError(f'unparseable JSON from LLM at step {step!r}')


class MockLLM:
    """Deterministic canned responses so --dry-run needs no server."""

    model = 'mock-llm'

    def preflight(self) -> str:
        return self.model

    def text(self, prompt: str, step: str = '', max_tokens: int | None = None) -> str:
        if step == 'synthesize':
            return (
                '## Overview\n\n'
                'Mock synthesis of tonight\'s findings on this question. '
                'Key claim drawn from [S1], with supporting detail from [S2]. '
                'Related concept: [[Mock Topic]].\n\n'
                '## Open threads\n\n- Mock follow-up thread.\n'
            )
        return 'ok'

    def json(self, prompt: str, step: str = '', fallback: dict | None = None) -> dict:
        canned = {
            'queries': {'queries': ['mock query one', 'mock query two']},
            'grade': {
                'relevant': True,
                'summary': 'Mock summary of the source.',
                'key_claims': ['Mock claim A', 'Mock claim B'],
                'quote': MOCK_EVIDENCE,
            },
            'frontier': {
                'close': [],
                'add': ['Mock follow-up question raised by tonight\'s findings?'],
            },
        }
        return canned.get(step, fallback or {})
