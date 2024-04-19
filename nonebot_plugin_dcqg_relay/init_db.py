import sqlite3
from pathlib import Path


async def init_db(dbpath: Path):
    conn = sqlite3.connect(dbpath)
    conn.execute(
        """CREATE TABLE ID (
            DCID    INT     NOT NULL,
            QQID    TEXT    NOT NULL
        );"""
    )
    return conn
