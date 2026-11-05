-- seed-test.sql — loopback lab data (NOT for deployment).
-- Same schema as ../kamailio/db/seed.sql, but all addresses are 127.0.0.x so
-- the edge + fakes + scripted UACs run on one loopback host.
--   hub/public edge socket : 127.0.0.1:5060
--   "overlay" edge socket  : 127.0.0.9:5060   (WGIP)
--   fake home1 voip-stack  : 127.0.0.1:5190   (dispatcher set 11)
--   fake home2 voip-stack  : 127.0.0.1:5191   (dispatcher set 12)
--   fake home1 PSTN gw     : 127.0.0.1:5291   (dispatcher set 31)
--   tenant "home N" sources: 127.0.0.(10+N)   (trusted table tags)
CREATE TABLE IF NOT EXISTS tenant_registry (
	tprefix CHAR PRIMARY KEY, tvalue CHAR NOT NULL
);
CREATE TABLE IF NOT EXISTS tenant_landline (
	tprefix CHAR PRIMARY KEY, tvalue CHAR NOT NULL
);
CREATE TABLE IF NOT EXISTS dispatcher (
	id INTEGER PRIMARY KEY AUTOINCREMENT, setid INTEGER NOT NULL DEFAULT 0,
	destination CHAR NOT NULL DEFAULT '', flags INTEGER NOT NULL DEFAULT 0,
	priority INTEGER NOT NULL DEFAULT 0, attrs CHAR NOT NULL DEFAULT '',
	description CHAR NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS dialplan (
	id INTEGER PRIMARY KEY AUTOINCREMENT, dpid INTEGER NOT NULL DEFAULT 0,
	pr INTEGER NOT NULL DEFAULT 1, match_op INTEGER NOT NULL DEFAULT 1,
	match_exp TEXT NOT NULL DEFAULT '', match_len INTEGER NOT NULL DEFAULT 0,
	subst_exp TEXT NOT NULL DEFAULT '^(.*)$', repl_exp TEXT NOT NULL DEFAULT '\1',
	attrs CHAR NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS version (
	table_name TEXT PRIMARY KEY, table_version INTEGER NOT NULL
);
INSERT OR REPLACE INTO version VALUES
	('dispatcher', 4), ('dialplan', 2), ('trusted', 6), ('address', 6);
CREATE TABLE IF NOT EXISTS address (
	id INTEGER PRIMARY KEY AUTOINCREMENT, grp INTEGER NOT NULL DEFAULT 0,
	ip_addr TEXT NOT NULL, mask INTEGER NOT NULL DEFAULT 32,
	port INTEGER NOT NULL DEFAULT 0, 	tag CHAR
);
CREATE TABLE IF NOT EXISTS trusted (
	id INTEGER PRIMARY KEY AUTOINCREMENT, src_ip CHAR NOT NULL,
	proto CHAR NOT NULL DEFAULT 'any', from_pattern CHAR,
	ruri_pattern CHAR, 	tag CHAR, priority INTEGER NOT NULL DEFAULT 0
);

INSERT OR REPLACE INTO tenant_registry (tprefix, tvalue) VALUES
	('sip.alpha.home.example.net', 'T1,11,31,01111111,Alpha,10,1+1+90'),
	('sip.beta.home.example.net', 'T2,12,32,02222222,Beta,10,1+1'),
	('sip.gamma.home.example.net', 'T3,13,0,03333333,Gamma,10,1+1'); -- no PSTN

INSERT OR REPLACE INTO tenant_landline (tprefix, tvalue)
VALUES ('01111111', 'T1,home,11,1'),
       ('02222222', 'T2,home,12,1');

INSERT INTO dispatcher (setid, destination, description) VALUES
	(11, 'sip:127.0.0.1:5190;transport=udp', 'lab home1 voip-stack'),
	(12, 'sip:127.0.0.1:5191;transport=udp', 'lab home2 voip-stack'),
	(13, 'sip:127.0.0.1:5193;transport=udp', 'lab home3 voip-stack'),
	(31, 'sip:127.0.0.1:5291;transport=udp', 'lab home1 PSTN gw'),
	(32, 'sip:127.0.0.1:5292;transport=udp', 'lab home2 PSTN gw');

INSERT INTO dialplan (dpid, pr, match_op, match_exp, attrs)
VALUES (10, 1, 1, '^(112)$', 'lab emergency'),
	(90, 1, 1, '^0900[0-9]{6}$', 'lab premium block');

INSERT INTO trusted (src_ip, proto, from_pattern, ruri_pattern, tag)
VALUES
	('127.0.0.11', 'any', NULL, NULL, 'sip.alpha.home.example.net'),
	('127.0.0.12', 'any', NULL, NULL, 'sip.beta.home.example.net'),
	('127.0.0.13', 'any', NULL, NULL, 'sip.gamma.home.example.net');
