"""Local, non-destructive engineering knowledge exports."""

from ekm.documents.excel import write_excel
from ekm.documents.word import write_word

__all__ = ["write_excel", "write_word"]
