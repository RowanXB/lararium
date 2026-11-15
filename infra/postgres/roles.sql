CREATE ROLE lararium_owner LOGIN PASSWORD :'owner_pw';
CREATE ROLE lararium_backend LOGIN PASSWORD :'backend_pw';
-- The webhook receiver's role: migration 0011 lets it insert into core.webhook_inbox, only.
CREATE ROLE lararium_webhook_in LOGIN PASSWORD :'webhook_in_pw';
CREATE DATABASE lararium OWNER lararium_owner;
\connect lararium
REVOKE ALL ON SCHEMA public FROM PUBLIC;
