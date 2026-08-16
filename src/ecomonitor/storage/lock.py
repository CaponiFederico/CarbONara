import logging
import os
from pathlib import Path
from types import TracebackType

#Lock fra processi sul file di database.

#Due processi che scrivono lo stesso database raddoppiano ogni evento e si
#sovrascrivono a vicenda lo stato del pool

#La GUI aveva gia' SingleInstanceGuar ma quello vive in ecomonitor.gui
#dipende da Qt e protegge l'app

#Qui il lock e' legato al DATABASE

logger = logging.getLogger(__name__)


class DatabaseLock:

    def __init__(self, db_path: Path | str) -> None:
        self.path = Path(f"{Path(db_path)}.lock")
        self._handle = None

    def acquire(self) -> bool:
        #False se un altro processo lo tiene gia'. Il chiamante deve uscire
        #senza toccare il database
        self.path.parent.mkdir(parents=True, exist_ok=True)
        #os.open e non open(): serve un descrittore su cui bloccare
        handle = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o644)

        try:
            _lock_exclusive_non_blocking(handle)
        except OSError:
            os.close(handle)
            return False
        except NotImplementedError:
            #piattaforma senza primitiva di lock
            os.close(handle)
            logger.warning(
                "Lock del database non supportato su questa piattaforma: due istanze "
                "contemporanee non verrebbero intercettate"
            )
            return True

        self._handle = handle
        #il PID rende diagnosticabile chi lo tiene, guardando il file
        os.truncate(handle, 0)
        os.write(handle, str(os.getpid()).encode("ascii"))
        os.fsync(handle)
        return True

    def release(self) -> None:
        if self._handle is None:
            return
        try:
            _unlock(self._handle)
        finally:
            os.close(self._handle)
            self._handle = None

    def __enter__(self) -> bool:
        return self.acquire()

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.release()


def _lock_exclusive_non_blocking(handle: int) -> None:
    #Solleva OSError se un altro processo tiene il lock
    #NotImplementedError se la piattaforma non offre 
    # una primitiva utilizzabile
    try:
        import fcntl
    except ImportError:
        pass
    else:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return

    try:
        import msvcrt
    except ImportError as exc: 
        raise NotImplementedError("nessuna primitiva di lock disponibile") from exc

    #su Windows il lock e' per intervallo di byte: ne basta uno
    msvcrt.locking(handle, msvcrt.LK_NBLCK, 1)


def _unlock(handle: int) -> None:
    try:
        import fcntl
    except ImportError:
        pass
    else:
        fcntl.flock(handle, fcntl.LOCK_UN)
        return

    try:
        import msvcrt
    except ImportError:  
        return
    try:
        os.lseek(handle, 0, os.SEEK_SET)
        msvcrt.locking(handle, msvcrt.LK_UNLCK, 1)
    except OSError:
        pass
