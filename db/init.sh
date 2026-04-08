#!/bin/bash
set -e

echo "Running migrations..."
for f in /docker-entrypoint-initdb.d/migrations/*.sql; do
  echo "  Applying $f"
  psql -U postgres -d bma_health -f "$f"
done

echo "Running seeds..."
for f in /docker-entrypoint-initdb.d/seeds/*.sql; do
  echo "  Applying $f"
  psql -U postgres -d bma_health -f "$f"
done

echo "Database initialization complete."
