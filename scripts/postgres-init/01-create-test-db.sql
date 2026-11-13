-- Creates a separate database for the automated integration test suite so that
-- tests never touch development data. Runs once, on first container start.
CREATE DATABASE trend_agent_test;
