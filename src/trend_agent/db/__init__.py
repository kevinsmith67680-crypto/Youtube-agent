"""Persistence layer (PostgreSQL via SQLAlchemy 2.x)."""

# The Alembic head this code expects. Readiness fails (and the services refuse to run
# scheduled work) while the database is at any other revision; a test keeps this constant
# equal to the migrations' head.
EXPECTED_SCHEMA_REVISION = "0011"
