"""Connection settings from the environment; defaults match docker-compose.yml."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

import psycopg
import pymysql


@dataclass(frozen=True)
class Settings:
    pg_dsn: str = "postgresql://taxi:taxi@127.0.0.1:5435/taxi"
    mysql_host: str = "127.0.0.1"
    mysql_port: int = 3309
    mysql_db: str = "taxi"
    data_dir: Path = Path("data")

    @classmethod
    def from_env(cls) -> Settings:
        e, d = os.environ, cls()
        return cls(
            pg_dsn=e.get("TAXI_PG_DSN", d.pg_dsn),
            mysql_host=e.get("TAXI_MYSQL_HOST", d.mysql_host),
            mysql_port=int(e.get("TAXI_MYSQL_PORT", d.mysql_port)),
            mysql_db=e.get("TAXI_MYSQL_DB", d.mysql_db),
            data_dir=Path(e.get("TAXI_DATA_DIR", str(d.data_dir))),
        )

    def pg(self, autocommit: bool = True) -> psycopg.Connection:
        conn = psycopg.connect(self.pg_dsn, autocommit=autocommit)
        conn.execute("SET TIME ZONE 'UTC'")
        return conn

    def mysql(self, local_infile: bool = False) -> pymysql.connections.Connection:
        return pymysql.connect(
            host=self.mysql_host,
            port=self.mysql_port,
            user="taxi",
            password="taxi",
            database=self.mysql_db,
            autocommit=True,
            local_infile=local_infile,
            init_command="SET time_zone = '+00:00'",
        )
