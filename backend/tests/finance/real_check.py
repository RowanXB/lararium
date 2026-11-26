"""What a real check (`make real-check`) may print of an error: the message of one of
Lararium's own displayable errors, whose text is written to be shown, masked; of any other
only its class (a database error's text quotes its SQL and parameters, a KeyError or
ValueError its input), and of a database error the class of the driver's error."""

from collections.abc import Callable

from sqlalchemy.exc import DBAPIError

from lararium.core.errors import DisplayableError


def shown(error: BaseException, mask: Callable[[str], str]) -> str:
    if isinstance(error, DisplayableError):
        return f"{type(error).__name__}: {mask(str(error))}"
    if isinstance(error, DBAPIError) and error.orig is not None:
        return type(error.orig).__name__
    return type(error).__name__
