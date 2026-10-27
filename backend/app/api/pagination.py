"""Resource catalogue page response."""
from typing import Generic, TypeVar

from pydantic import BaseModel

Item = TypeVar('Item')


class ResourcePage(BaseModel, Generic[Item]):
    items: list[Item]
    total: int
    page: int
    page_size: int


def page_result(items, total, page, page_size):
    return {'items': items, 'total': total, 'page': page, 'page_size': page_size}
