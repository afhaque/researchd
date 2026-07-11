"""Tavily search adapter. Requires TAVILY_API_KEY in the environment."""

import os

import requests

from . import SearchAdapter, SearchResult


class TavilyAdapter(SearchAdapter):
    name = 'tavily'

    def __init__(self):
        self.api_key = os.environ.get('TAVILY_API_KEY', '')
        if not self.api_key:
            raise RuntimeError('TAVILY_API_KEY not set')

    def search(self, query: str, limit: int = 5) -> list[SearchResult]:
        resp = requests.post(
            'https://api.tavily.com/search',
            json={
                'api_key': self.api_key,
                'query': query,
                'max_results': limit,
                'include_raw_content': True,
            },
            timeout=60,
        )
        resp.raise_for_status()
        out = []
        for r in resp.json().get('results', []):
            content = r.get('raw_content') or r.get('content') or ''
            if r.get('url') and content:
                out.append(SearchResult(
                    url=r['url'],
                    title=r.get('title', r['url']),
                    content=content,
                ))
        return out
