CREATE ROLE lararium_owner LOGIN PASSWORD :'owner_pw';
CREATE ROLE lararium_backend LOGIN PASSWORD :'backend_pw';
CREATE DATABASE lararium OWNER lararium_owner;
\connect lararium
REVOKE ALL ON SCHEMA public FROM PUBLIC;
