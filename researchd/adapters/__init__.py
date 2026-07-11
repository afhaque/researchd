"""Search adapters. Each returns a list of SearchResult with content included."""

import zlib
from dataclasses import dataclass

# Shared by MockAdapter content and MockLLM's canned grade: the pipeline's
# quote-in-source check only passes in --dry-run because both use this string.
MOCK_EVIDENCE = 'mock evidence sentence'


@dataclass
class SearchResult:
    url: str
    title: str
    content: str
    source_type: str = 'web'


class SearchAdapter:
    name = 'base'

    def search(self, query: str, limit: int = 5) -> list[SearchResult]:
        raise NotImplementedError


class MockAdapter(SearchAdapter):
    name = 'mock'

    def search(self, query: str, limit: int = 5) -> list[SearchResult]:
        return [
            SearchResult(
                url=f'https://example.org/mock/{zlib.crc32(query.encode()) % 10000}/{i}',
                title=f'Mock result {i} for: {query}',
                content=(f'This is mock source content for query {query!r}. '
                         f'It contains the {MOCK_EVIDENCE} needed for '
                         'the quote check, plus filler text.'),
            )
            for i in range(1, min(limit, 2) + 1)
        ]


def get_adapter(name: str) -> SearchAdapter:
    if name == 'mock':
        return MockAdapter()
    if name == 'tavily':
        from .tavily import TavilyAdapter
        return TavilyAdapter()
    if name == 'pubmed':
        from .pubmed import PubMedAdapter
        return PubMedAdapter()
    raise ValueError(f'unknown adapter: {name}')
