
import os
import sys
import time
import sqlite3
import threading
from multiprocessing import Lock

class SQLite3Backend(object):
    PY2 = sys.version_info < (3, 0)
    CHECKP_INTERVAL = 90 # [sec] WAL checkpoint

    def __init__(self, sqlFile = None, readonly=True):
        assert(isinstance(sqlFile, str))
        assert(isinstance(readonly, bool))
        self._sqlFile = sqlFile   # Path to database file.
        self._readonly = bool(readonly)
        self._lock = Lock()       # Database lock.
        self._conn = {}           # Database connection.
        self._lastCheckP = int(0) # Epoch of last checkpoint.

    def __enter__(self):
        self._lastCheckP = time.time()
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self._db_disconnect()

    def __str__(self):
        return self._sqlFile

    @property
    def readonly(self):
        return self._readonly

    # PUBLIC METHODS
    def execute_read(self, dbCmd = None, params = (), CB = None):
        with self._lock:
            c = self._get_conn().cursor()
            for row in c.execute(dbCmd, params):
                CB(row)
            c.close()

    def execute_write(self, dbCmd = None, params = (), commit=True):
        with self._lock:
            conn = self._get_conn()
            if dbCmd is not None:
                c = conn.cursor()
                c.execute(dbCmd, params)
                c.close()
            if bool(commit) is True:
                conn.commit()
                self._wal_throttled()

    def wal_throttled_threadsafe(self):
        with self._lock:
            self._wal_throttled()

    def wal_checkpoint_threadsafe(self):
        """
        Store checkpoint: forces database modifications to be persistent.
        Automatically done when sqlite cache runs over the 1000 pages threshold.
        IMPORTANT: slow operation, manual syncs are only useful for sporadic
        transactions that you really want to survive a power loss.
        """
        with self._lock:
            self._wal_checkpoint()
    
    # PRIVATE METHODS
    def _wal_throttled(self):
        # commits still require a wal_checkpoint to become persistent.
        if abs(time.time() - self._lastCheckP) < self.CHECKP_INTERVAL:
            return
        self._wal_checkpoint()

    def _wal_checkpoint(self):
        self._lastCheckP = time.time()
        c = self._get_conn().cursor()
        c.execute('PRAGMA wal_checkpoint')
        c.close()

    def _db_connect(self):
        CID = SQLite3Backend._getCID()
        assert CID not in self._conn
        if SQLite3Backend.PY2:
            self._db_connect_py2(CID)
        else:
            self._conn[CID] = sqlite3.connect(
                'file:{:s}?immutable={:s}'.format(self._sqlFile, '1' if self.readonly else '0'),
                detect_types = sqlite3.PARSE_DECLTYPES, # so datetimes won't be BLOBs
                check_same_thread = False,
                uri = True,
            )
        c = self._get_conn().cursor()
        if self.readonly is True:
            c.execute('PRAGMA query_only=1')
        else:
            c.execute('PRAGMA journal_mode=WAL')
            c.execute('PRAGMA synchronous=NORMAL')

    # Legacy support for Python<3.x.
    def _db_connect_py2(self, CID):
        if os.access(self._sqlFile, os.W_OK) is False:
            raise Exception('Python>=3.x is required for immutable sqlite3 database.')
        self._conn[CID] = sqlite3.connect(
            self._sqlFile,
            detect_types = sqlite3.PARSE_DECLTYPES,
            check_same_thread = False
        )
        self._conn[CID].text_factory = bytes

    def _db_disconnect(self):
        # Commit, checkpoint.
        if self.readonly is False:
            self.wal_checkpoint_threadsafe()
        # Close all connections to database.
        for CID in self._conn:
            self._conn[CID].close()
        # Remove all items from dict.
        self._conn.clear()

    def _get_conn(self):
        if self._lock.acquire(False) is True:
            self._lock.release()
            raise Exception('Forgot to lock?')
        # sqlite3 multithreading: http://beets.io/blog/sqlite-nightmare.html
        CID = SQLite3Backend._getCID()
        try:
            return self._conn[CID]
        except KeyError:
            self._db_connect()
            return self._conn[CID]

    @staticmethod
    def _getCID():
        return threading.current_thread().ident
