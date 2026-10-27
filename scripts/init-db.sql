-- Create the AgentScope runtime database (product layer uses the
-- default POSTGRES_DB). Idempotent: uses IF NOT EXISTS via DO block.
SELECT 'CREATE DATABASE workmate_agentscope'
WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = 'workmate_agentscope')\gexec
