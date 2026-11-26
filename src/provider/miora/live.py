"""排队后重读当前账号票据，不把新身份混入旧账号的节流名额。"""

from collections.abc import Callable

from ...compat.openai.request import InvalidRequest
from ...request_context import get_credential_id
from .credential import MioraCredential

CredentialReader = Callable[[], dict | None]


def current_credential(
    original: MioraCredential, reader: CredentialReader | None
) -> MioraCredential:
    if reader is None or not get_credential_id():
        return original
    data = reader()
    if data is None:
        raise InvalidRequest("Miora credential no longer exists")
    current = MioraCredential.from_import(data)
    if current.identity() != original.identity():
        raise InvalidRequest("Miora account identity changed while this request was queued")
    return current
