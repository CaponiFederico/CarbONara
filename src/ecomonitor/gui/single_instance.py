import hashlib
import logging
from collections.abc import Callable
from pathlib import Path

from PySide6.QtCore import QObject
from PySide6.QtNetwork import QLocalServer, QLocalSocket

#Due processi sullo stesso database contano ogni ricarica due volte e si
#sovrascrivono lo stato del pool a turno: scenario tipico di chi crede di aver
#chiuso l'app (tray) e la riavvia. Il QLocalServer ha il nome derivato dal
#percorso dei dati, cosi' il conflitto e' esattamente quello che conta, e la
#seconda istanza puo' chiedere alla prima di mostrarsi

logger = logging.getLogger(__name__)

CONNECT_TIMEOUT_MS = 400  # si paga una volta sola all'avvio


def _server_name(data_path: Path) -> str:
    #un digest e non il percorso: i nomi dei socket hanno limiti di lunghezza e
    #caratteri diversi fra named pipe di Windows e socket file Unix
    digest = hashlib.sha256(str(data_path.resolve()).encode("utf-8")).hexdigest()
    return f"ecomonitor-{digest[:16]}"


class SingleInstanceGuard(QObject):
    def __init__(self, data_path: Path, on_second_launch: Callable[[], None] | None = None) -> None:
        super().__init__()
        self._name = _server_name(data_path)
        self._on_second_launch = on_second_launch
        self._server: QLocalServer | None = None

    def try_acquire(self) -> bool:
        #False se un'altra istanza e' gia' viva: le viene chiesto di mostrarsi,
        #e il chiamante deve uscire senza toccare il database
        if self._notify_existing_instance():
            return False

        #su Unix un processo terminato male lascia il socket file orfano e
        #listen() fallirebbe per sempre: qui e' certamente abbandonato, perche'
        #sopra nessuno ha risposto
        QLocalServer.removeServer(self._name)

        server = QLocalServer(self)
        if not server.listen(self._name):
            logger.warning(
                "Impossibile creare il lock di istanza singola (%s): l'applicazione parte "
                "comunque, ma un secondo avvio non verrebbe intercettato",
                server.errorString(),
            )
            return True

        server.newConnection.connect(self._handle_second_launch)
        self._server = server
        return True

    def release(self) -> None:
        if self._server is not None:
            self._server.close()
            QLocalServer.removeServer(self._name)
            self._server = None

    # -- interni -----------------------------------------------------------

    def _notify_existing_instance(self) -> bool:
        socket = QLocalSocket()
        socket.connectToServer(self._name)
        if not socket.waitForConnected(CONNECT_TIMEOUT_MS):
            return False

        socket.write(b"show")
        socket.flush()
        socket.waitForBytesWritten(CONNECT_TIMEOUT_MS)
        socket.disconnectFromServer()
        return True

    def _handle_second_launch(self) -> None:
        if self._server is None:
            return
        connection = self._server.nextPendingConnection()
        if connection is not None:
            connection.readyRead.connect(connection.close)
        logger.info("Secondo avvio intercettato: riporto in primo piano la finestra esistente")
        if self._on_second_launch is not None:
            self._on_second_launch()
