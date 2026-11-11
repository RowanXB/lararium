CREATE ROLE fg_owner LOGIN PASSWORD :'owner_pw';
CREATE ROLE fg_backend LOGIN PASSWORD :'backend_pw';
CREATE DATABASE financial_gate OWNER fg_owner;
\connect financial_gate
REVOKE ALL ON SCHEMA public FROM PUBLIC;
