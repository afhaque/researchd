"""LLM clients: Anthropic (cloud), LM Studio (local, OpenAI-compatible), and a
mock for dry runs. All speak one interface — preflight() / text() / json() —
so the pipeline is provider-agnostic and you switch by editing config.yaml.

Design rule from red-teaming: every call is bounded (timeout + max_tokens),
JSON is parsed defensively (fence-strip, one retry with the error, then a
caller-supplied fallback) so one bad generation never kills the night.

Per-step model routing: text()/json() take a `step` (queries/grade/synthesize/
frontier). A client maps step → model, so a cloud run can grade with a cheap
model and synthesize with a strong one. Local runs use one loaded model.
"""

import json
import os
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


class BaseLLM:
    """Shared step→model routing and defensive JSON parsing. Subclasses
    implement preflight() and _complete(prompt, model, max_tokens)."""

    def __init__(self, cfg: dict):
        self.max_tokens = cfg.get('max_tokens', 2048)
        self.timeout = cfg.get('timeout_seconds', 180)
        self.default_model = ''
        self.step_models: dict = {}

    def _model_for(self, step: str) -> str:
        return self.step_models.get(step, self.default_model)

    def _complete(self, prompt: str, model: str, max_tokens: int) -> str:
        raise NotImplementedError

    def preflight(self) -> str:
        raise NotImplementedError

    def text(self, prompt: str, step: str = '', max_tokens: int | None = None) -> str:
        return self._complete(prompt, self._model_for(step),
                              max_tokens or self.max_tokens)

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


class AnthropicClient(BaseLLM):
    """Calls the Anthropic Messages API directly over HTTPS (no SDK — keeps the
    dependency set to requests + pyyaml). Temperature is intentionally omitted:
    the frontier models (Sonnet 5, Opus 4.8, Fable 5) reject it with a 400."""

    ENDPOINT = 'https://api.anthropic.com/v1/messages'
    API_VERSION = '2023-06-01'

    def __init__(self, cfg: dict):
        super().__init__(cfg)
        acfg = cfg.get('anthropic') or {}
        self.default_model = acfg.get('default_model', 'claude-sonnet-5')
        self.step_models = acfg.get('step_models') or {}
        self.api_key_env = acfg.get('api_key_env', 'ANTHROPIC_API_KEY')
        self.api_key = os.environ.get(self.api_key_env, '')

    def preflight(self) -> str:
        if not self.api_key:
            raise LLMError(f'{self.api_key_env} not set in environment')
        self._complete('Reply with the single word: ok', self.default_model, 8)
        return f'anthropic:{self.default_model}'

    def _complete(self, prompt: str, model: str, max_tokens: int) -> str:
        resp = requests.post(
            self.ENDPOINT,
            headers={
                'x-api-key': self.api_key,
                'anthropic-version': self.API_VERSION,
                'content-type': 'application/json',
            },
            json={
                'model': model,
                'max_tokens': max_tokens,
                'messages': [{'role': 'user', 'content': prompt}],
            },
            timeout=self.timeout,
        )
        resp.raise_for_status()
        data = resp.json()
        if data.get('stop_reason') == 'refusal':
            raise LLMError(f'Anthropic refused request (model={model})')
        for block in data.get('content', []):
            if block.get('type') == 'text':
                return block.get('text', '')
        return ''


class LMStudioClient(BaseLLM):
    """OpenAI-compatible local endpoint (LM Studio, Ollama, llama.cpp, vLLM).
    One loaded model serves every step, so step routing is a no-op here."""

    def __init__(self, cfg: dict):
        super().__init__(cfg)
        ocfg = cfg.get('openai') or {}
        self.base_url = ocfg.get('base_url', 'http://localhost:1234/v1').rstrip('/')
        self.default_model = ocfg.get('model') or ''
        self.temperature = cfg.get('temperature', 0.3)

    def preflight(self) -> str:
        resp = requests.get(f'{self.base_url}/models', timeout=30)
        resp.raise_for_status()
        models = [m['id'] for m in resp.json().get('data', [])]
        if not models:
            raise LLMError('LM Studio is running but no model is loaded')
        if self.default_model and self.default_model not in models:
            raise LLMError(
                f'model {self.default_model!r} not loaded; available: {models}')
        self.default_model = self.default_model or models[0]
        self._complete('Reply with the single word: ok', self.default_model, 8)
        return f'lmstudio:{self.default_model}'

    def _complete(self, prompt: str, model: str, max_tokens: int) -> str:
        resp = requests.post(
            f'{self.base_url}/chat/completions',
            json={
                'model': model,
                'messages': [{'role': 'user', 'content': prompt}],
                'max_tokens': max_tokens,
                'temperature': self.temperature,
            },
            timeout=self.timeout,
        )
        resp.raise_for_status()
        return resp.json()['choices'][0]['message']['content']


class MockLLM(BaseLLM):
    """Deterministic canned responses so --dry-run needs no server or key."""

    def __init__(self):
        super().__init__({})
        self.default_model = 'mock-llm'

    def preflight(self) -> str:
        return self.default_model

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


def make_llm(cfg: dict, dry_run: bool) -> BaseLLM:
    """Build the LLM client for this run. Dry runs are always mocked."""
    if dry_run:
        return MockLLM()
    llm_cfg = cfg['llm']
    provider = llm_cfg.get('provider', 'anthropic')
    if provider == 'anthropic':
        return AnthropicClient(llm_cfg)
    if provider in ('openai', 'lmstudio', 'local'):
        return LMStudioClient(llm_cfg)
    raise LLMError(f'unknown llm provider: {provider!r}')
