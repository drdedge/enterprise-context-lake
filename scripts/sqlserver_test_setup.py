"""Create a SQL Server test database and a lake account with the same rights as production (ADR-010).

The production account on BSPAI has db_ddladmin (plus data read/write) and nothing else: no db_owner,
no ALTER ANY SECURITY POLICY. This script recreates that on a disposable SQL Server (a local container
or the CI service) so the SQL Server tests exercise the real permission set:

    docker run -d -e ACCEPT_EULA=Y -e MSSQL_SA_PASSWORD=... -p 1433:1433 mcr.microsoft.com/mssql/server:2022-latest
    MSSQL_SA_PASSWORD=... python scripts/sqlserver_test_setup.py
    export LAKE_TEST_SQLSERVER_URL=...   # printed by this script

Never point it at a shared server: it creates a database, a login and a user.
"""

from __future__ import annotations

import os
import sys
import time

import pyodbc

HOST = os.environ.get("MSSQL_HOST", "127.0.0.1")
PORT = os.environ.get("MSSQL_PORT", "1433")
SA_PASSWORD = os.environ["MSSQL_SA_PASSWORD"]
DATABASE = os.environ.get("MSSQL_TEST_DATABASE", "BSPAI")
LAKE_USER = os.environ.get("MSSQL_LAKE_USER", "lake_svc")
LAKE_PASSWORD = os.environ.get("MSSQL_LAKE_PASSWORD", "Lake_Svc_Test_1")
DRIVER = os.environ.get("MSSQL_ODBC_DRIVER", "ODBC Driver 18 for SQL Server")


def connect(database: str = "master") -> pyodbc.Connection:
    dsn = (
        f"DRIVER={{{DRIVER}}};SERVER={HOST},{PORT};DATABASE={database};UID=sa;PWD={SA_PASSWORD};"
        "TrustServerCertificate=yes"
    )
    for attempt in range(60):  # the container takes a few seconds to accept logins
        try:
            return pyodbc.connect(dsn, autocommit=True)
        except pyodbc.Error:
            if attempt == 59:
                raise
            time.sleep(2)
    raise RuntimeError("unreachable")


def main() -> int:
    cur = connect().cursor()
    if cur.execute("SELECT DB_ID(?)", DATABASE).fetchone()[0] is None:
        cur.execute(f"CREATE DATABASE [{DATABASE}]")
    cur.execute(f"ALTER DATABASE [{DATABASE}] SET COMPATIBILITY_LEVEL = 150")  # as on the production server
    if cur.execute("SELECT 1 FROM sys.server_principals WHERE name = ?", LAKE_USER).fetchone() is None:
        cur.execute(f"CREATE LOGIN [{LAKE_USER}] WITH PASSWORD = '{LAKE_PASSWORD}', CHECK_POLICY = OFF")
    db = connect(DATABASE).cursor()
    if db.execute("SELECT 1 FROM sys.database_principals WHERE name = ?", LAKE_USER).fetchone() is None:
        db.execute(f"CREATE USER [{LAKE_USER}] FOR LOGIN [{LAKE_USER}]")
    for role in ("db_ddladmin", "db_datareader", "db_datawriter"):
        db.execute(f"ALTER ROLE {role} ADD MEMBER [{LAKE_USER}]")
    url = (
        f"mssql+aioodbc://{LAKE_USER}:{LAKE_PASSWORD}@{HOST}:{PORT}/{DATABASE}"
        f"?driver={DRIVER.replace(' ', '+')}&TrustServerCertificate=yes"
    )
    print(url)
    return 0


if __name__ == "__main__":
    sys.exit(main())
