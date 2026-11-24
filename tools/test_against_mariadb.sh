#!/bin/bash
# Dialekt-Test: volle pytest-Suite gegen MariaDB (Pi, Test-Schema).
#
# SQLite (Default der Suite) validiert anderen SQL-Dialekt als die
# Produktion — diese hier ist die Paritäts-Wahrheit. Vor Pushs auf
# Schema-/DB-Änderungen laufen lassen.
#
# Voraussetzung: SSH-Alias raspberry5-01. Das Schema wird PRO LAUF frisch
# angelegt (DROP/CREATE), Parallelität mit anderen Läufen gibt es nicht.
set -u
cd "$(dirname "$0")/.."

PI=raspberry5-01
PORT=3307
PW="${WB_MYSQL_PW_TEST:-wbtest-local-only}"

# 1) Tunnel (wiederverwenden, wenn schon einer lauscht)
if ! netstat -ano | grep -q ":$PORT .*LISTEN"; then
  ssh -N -L $PORT:127.0.0.1:3306 "$PI" &
  TUNNEL=$!
  sleep 4
else
  TUNNEL=""
fi

# 2) Test-Schema frisch aufsetzen
ssh "$PI" "sudo mariadb -e \"DROP DATABASE IF EXISTS woerterbuch_test;
          CREATE DATABASE woerterbuch_test CHARACTER SET utf8mb4
          COLLATE utf8mb4_unicode_ci;\""

# 2b) Tabellen aus SCHEMA_MYSQL erzeugen
WB_DB=mysql WB_MYSQL_HOST=127.0.0.1 WB_MYSQL_PORT=$PORT \
WB_MYSQL_USER=wbtest WB_MYSQL_PW="$PW" WB_MYSQL_DB=woerterbuch_test \
python -c "
import app.db as db
conn = db.MyConn()
conn.executescript(db.SCHEMA_MYSQL)
conn.commit()
print('Test-Schema: Tabellen erzeugt')"

# 3) Suite gegen MariaDB
WB_DB=mysql WB_MYSQL_HOST=127.0.0.1 WB_MYSQL_PORT=$PORT \
WB_MYSQL_USER=wbtest WB_MYSQL_PW="$PW" WB_MYSQL_DB=woerterbuch_test \
  python -m pytest tests/ -q
RC=$?

# 4) Aufräumen: Schema wegwerfen, eigenen Tunnel schließen
ssh "$PI" "sudo mariadb -e 'DROP DATABASE IF EXISTS woerterbuch_test;'" \
  || echo "WARN: Test-Schema nicht abgeräumt"
[ -n "$TUNNEL" ] && kill "$TUNNEL" 2>/dev/null
exit $RC
