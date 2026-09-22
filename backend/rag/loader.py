from urllib.parse import urlparse

import httpx
from bs4 import BeautifulSoup

from backend.rag.base import RagDocument


class HttpDocumentLoader:
    def __init__(self, timeout_seconds: float = 20.0) -> None:
        self.timeout_seconds = timeout_seconds

    async def load(self, url: str) -> RagDocument:
        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("RAG source must be an absolute HTTP(S) URL")
        async with httpx.AsyncClient(
            follow_redirects=True,
            timeout=self.timeout_seconds,
            headers={"User-Agent": "CarAnalyzer/0.1"},
        ) as client:
            response = await client.get(url)
            response.raise_for_status()
        soup = BeautifulSoup(response.content, "lxml")
        for tag in soup(["script", "style", "noscript"]):
            tag.decompose()
        title = soup.title.get_text(" ", strip=True) if soup.title else parsed.hostname
        return RagDocument(
            source_url=str(response.url),
            source_title=title,
            content=soup.get_text(" ", strip=True),
        )
