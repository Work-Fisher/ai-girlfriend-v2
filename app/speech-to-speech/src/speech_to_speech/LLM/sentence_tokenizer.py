"""Sentence splitting that never downloads data at runtime."""

from nltk.tokenize import PunktSentenceTokenizer


_TOKENIZER = PunktSentenceTokenizer()


def sent_tokenize(text: str) -> list[str]:
    """Split streaming text without depending on a user NLTK cache."""

    return _TOKENIZER.tokenize(text)
