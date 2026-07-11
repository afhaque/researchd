"""PubMed adapter via NCBI E-utilities (free, no key required at low volume).

esearch finds PMIDs for a query; efetch pulls titles + abstracts. Abstracts
are the content the pipeline grades — good enough for a nightly first pass,
and they carry publication types for evidence-level tagging.
"""

import time
import xml.etree.ElementTree as ET

import requests

from . import SearchAdapter, SearchResult

EUTILS = 'https://eutils.ncbi.nlm.nih.gov/entrez/eutils'


class PubMedAdapter(SearchAdapter):
    name = 'pubmed'

    def search(self, query: str, limit: int = 5) -> list[SearchResult]:
        es = requests.get(f'{EUTILS}/esearch.fcgi', params={
            'db': 'pubmed', 'term': query, 'retmax': limit,
            'retmode': 'json', 'sort': 'relevance',
        }, timeout=30)
        es.raise_for_status()
        pmids = es.json().get('esearchresult', {}).get('idlist', [])
        if not pmids:
            return []
        time.sleep(0.4)  # NCBI asks for <=3 req/s without an API key
        ef = requests.get(f'{EUTILS}/efetch.fcgi', params={
            'db': 'pubmed', 'id': ','.join(pmids), 'retmode': 'xml',
        }, timeout=60)
        ef.raise_for_status()
        out = []
        root = ET.fromstring(ef.text)
        for article in root.iter('PubmedArticle'):
            pmid = article.findtext('.//PMID', '')
            title = article.findtext('.//ArticleTitle', '') or f'PMID {pmid}'
            abstract = ' '.join(
                (el.text or '') for el in article.findall('.//AbstractText'))
            pub_types = [el.text or '' for el in
                         article.findall('.//PublicationType')]
            if pmid and abstract:
                out.append(SearchResult(
                    url=f'https://pubmed.ncbi.nlm.nih.gov/{pmid}/',
                    title=title,
                    content=f'[Publication types: {", ".join(pub_types)}]\n'
                            f'{abstract}',
                    source_type='pubmed',
                ))
        return out
