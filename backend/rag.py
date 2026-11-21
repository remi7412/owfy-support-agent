import math
import re
from collections import Counter
from typing import Any, Dict, List

STOPWORDS = {
    "a", "an", "and", "are", "as", "at", "be", "by", "for", "from", "has", "he",
    "in", "is", "it", "its", "of", "on", "that", "the", "to", "was", "were",
    "will", "with", "we", "you", "your", "our", "can", "do", "how", "what",
    "when", "where", "who", "why", "this", "they", "or", "if", "not", "i", "me",
}


def tokenize(text: str) -> List[str]:
    tokens = re.findall(r"[a-z0-9]+", text.lower())
    return [t for t in tokens if t not in STOPWORDS and len(t) > 1]


def chunk_text(text: str, max_chars: int = 600) -> List[str]:
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    chunks: List[str] = []
    for p in paragraphs:
        if len(p) <= max_chars:
            chunks.append(p)
        else:
            sentences = re.split(r"(?<=[.!?])\s+", p)
            buf = ""
            for s in sentences:
                if len(buf) + len(s) + 1 <= max_chars:
                    buf = f"{buf} {s}".strip()
                else:
                    if buf:
                        chunks.append(buf)
                    buf = s
            if buf:
                chunks.append(buf)
    return chunks or [text.strip()]


def retrieve_relevant_chunks(
    query: str,
    docs: List[Dict[str, Any]],
    top_k: int = 3,
    min_score: float = 0.05,
) -> List[Dict[str, Any]]:
    query_tokens = tokenize(query)
    if not query_tokens or not docs:
        return []

    corpus_chunks = []
    for doc in docs:
        for chunk in chunk_text(doc["content"]):
            tokens = tokenize(f"{doc['title']} {doc['category']} {chunk}")
            corpus_chunks.append({
                "doc_id": doc["id"],
                "title": doc["title"],
                "category": doc["category"],
                "content": chunk,
                "tokens": tokens,
            })

    if not corpus_chunks:
        return []

    n_docs = len(corpus_chunks)
    df = Counter()
    for item in corpus_chunks:
        for t in set(item["tokens"]):
            df[t] += 1

    idf = {t: math.log((n_docs + 1) / (freq + 1)) + 1.0 for t, freq in df.items()}

    def tfidf_vec(tokens: List[str]) -> Dict[str, float]:
        tf = Counter(tokens)
        total = max(len(tokens), 1)
        return {t: (count / total) * idf.get(t, 1.0) for t, count in tf.items()}

    q_vec = tfidf_vec(query_tokens)
    q_norm = math.sqrt(sum(v * v for v in q_vec.values())) or 1.0

    scored = []
    for item in corpus_chunks:
        d_vec = tfidf_vec(item["tokens"])
        d_norm = math.sqrt(sum(v * v for v in d_vec.values())) or 1.0
        dot = sum(q_vec[t] * d_vec.get(t, 0.0) for t in q_vec)
        score = dot / (q_norm * d_norm)
        if score >= min_score:
            scored.append({
                "doc_id": item["doc_id"],
                "title": item["title"],
                "category": item["category"],
                "content": item["content"],
                "score": round(score, 4),
            })

    scored.sort(key=lambda x: x["score"], reverse=True)
    return scored[:top_k]
