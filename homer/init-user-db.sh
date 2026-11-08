#!/bin/bash
set -e

# heplify-server creates homer_data on first start; homer-app needs the
# config database to exist before it connects.
psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" <<-EOSQL
	CREATE DATABASE homer_config;
EOSQL
