import re
import sys
from collections.abc import Sequence

#Chiede all'istanza in esecuzione di mostrare la finestra

PIPE_FLAG = "--pipe"

#solo i nomi che SingleInstanceGuard crea
PIPE_NAME_PATTERN = re.compile(r"ecomonitor-[0-9a-f]{16}")


def pipe_name_from(arguments: Sequence[str]) -> str | None:
    for index, argument in enumerate(arguments[:-1]):
        if argument == PIPE_FLAG:
            name = arguments[index + 1]
            return name if PIPE_NAME_PATTERN.fullmatch(name) else None
    return None


def request_window(pipe_name: str) -> bool:
    try:
        with open(rf"\\.\pipe\{pipe_name}", "wb"):
            pass
    except OSError:
        return False  
        # applicazione non in esecuzione: non c'e' niente da mostrare
    return True


def main(arguments: Sequence[str] | None = None) -> int:
    name = pipe_name_from(sys.argv[1:] if arguments is None else arguments)
    if name is None:
        return 2
    return 0 if request_window(name) else 1


if __name__ == "__main__":
    raise SystemExit(main())
