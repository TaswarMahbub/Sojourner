#!/bin/sh
# Local database only: give the two API roles logins, with passwords from backend/cloud/.env.
# Runs once, on the first start of an empty volume.
set -eu
: "${APP_DB_PASSWORD:?set it in backend/cloud/.env}"
: "${DASHBOARD_DB_PASSWORD:?set it in backend/cloud/.env}"

psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" \
     -v app_pw="$APP_DB_PASSWORD" -v dash_pw="$DASHBOARD_DB_PASSWORD" <<'SQL'
ALTER ROLE sojourner_app LOGIN PASSWORD :'app_pw';
SELECT 'CREATE ROLE demo_dashboard LOGIN IN ROLE demo_reader'
 WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'demo_dashboard') \gexec
ALTER ROLE demo_dashboard LOGIN PASSWORD :'dash_pw';
SQL
