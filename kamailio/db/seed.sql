-- seed.sql — sqlite schema + EXAMPLE seed data for the VoIP Stack edge proxy
-- ============================================================================
-- Reference implementation runs WITHOUT an external DBMS. To switch to MySQL/
-- PostgreSQL later: point db_url modparams in kamailio.cfg at the real server
-- (kamailio -A 'DBURL="mysql://user:pass@host/voip"'), keep table/column names
-- identical, and use kamdbctl to create them. See schema.md.
--
-- The rows below are EXAMPLES for two fictional tenants, clearly marked.
-- Adding a real home = data rows only (README.md §Onboarding), never edits
-- of kamailio.cfg. Tunnel addresses/resources must match the Pangolin setup.
-- ============================================================================

PRAGMA journal_mode=DELETE;

-- ---------------------------------------------------------------------------
-- tenant_registry: mtree "tenantdom" — SIP domain -> packed tenant record.
-- Default 2-column mtree load; tvalue holds the packed record, comma-joined:
--   tvalue = tenantid,ds_onnet,ds_pstn,cli,label,dpid,flags
-- where the fields are:
--   tenantid  opaque short id for logs/lookups
--   ds_onnet  dispatcher set id of this home's voip-stack (tunnel address)
--   ds_pstn   dispatcher set id of this home's PSTN gateway(s); 0 = no trunk
--   cli       landline number AS DIALED LOCALLY (no E.164 assumption)
--   label     optional display name for inter-home From rewriting (no commas!)
--   dpid      dialplan partition id holding this home's emergency patterns
--   flags     "ih_on+pstn_on+block_dpid" (third part optional):
--               ih_on      = 1: tenant participates in inter-home calling
--               pstn_on    = 1: on-net inter-home failure may fall back to PSTN
--               block_dpid = dialplan partition with BLOCKED destinations
--                            (premium/audiotex etc.); 0 or absent = none
-- (Columns are CHAR: db_sqlite maps only CHAR/CLOB to the DB1_STRING type
--  mtree requires.)
CREATE TABLE IF NOT EXISTS tenant_registry (
	tprefix CHAR PRIMARY KEY,
	tvalue  CHAR NOT NULL
);

-- ---------------------------------------------------------------------------
-- tenant_landline: mtree "landline" — dialed digits -> packed target record.
-- Longest-prefix match: full numbers and delegated ranges can coexist.
-- Landlines are stored IN LOCAL FORMAT AS DIALED — no country conventions.
--   tvalue = tenantid,target,ds_onnet,ih_on
--     tenantid  target tenant id (matches tenant_registry.tenantid)
--     target    ring group or extension name the R-URI user is rewritten to
--     ds_onnet  dispatcher set id of the TARGET home's voip-stack
--     ih_on     1 = this landline accepts inter-home calls
CREATE TABLE IF NOT EXISTS tenant_landline (
	tprefix CHAR PRIMARY KEY,
	tvalue  CHAR NOT NULL
);

-- ---------------------------------------------------------------------------
-- dispatcher: standard Kamailio table. Two set ids per tenant:
--   on-net set  -> voip-stack tunnel address of that home (UDP 5060)
--   PSTN set    -> that home's PSTN path; in this deployment: voip-stack
--                  itself (HT813 stays LAN-only, pangolin/SETUP.md section 4).
--                  Empty set / set id 0 in tenant_registry = no PSTN trunk.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS dispatcher (
	id          INTEGER PRIMARY KEY AUTOINCREMENT,
	setid       INTEGER NOT NULL DEFAULT 0,
	destination CHAR NOT NULL DEFAULT '',
	flags       INTEGER NOT NULL DEFAULT 0,
	priority    INTEGER NOT NULL DEFAULT 0,
	attrs       CHAR NOT NULL DEFAULT '',
	description CHAR NOT NULL DEFAULT ''
);

-- ---------------------------------------------------------------------------
-- dialplan: emergency rules (match-only; identity rewrite). Per-country
-- patterns are configured by ROWS: pick a dpid per country profile and point
-- tenant_registry.dpid at it. NOTHING emergency-related is in kamailio.cfg.
-- match_op 1 = regex; match_len 0; subst/repl = identity.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS dialplan (
	id        INTEGER PRIMARY KEY AUTOINCREMENT,
	dpid      INTEGER NOT NULL DEFAULT 0,
	pr        INTEGER NOT NULL DEFAULT 1,
	match_op  INTEGER NOT NULL DEFAULT 1,
	match_exp TEXT NOT NULL DEFAULT '',
	match_len INTEGER NOT NULL DEFAULT 0,
	subst_exp TEXT NOT NULL DEFAULT '^(.*)$',
	repl_exp  TEXT NOT NULL DEFAULT '\1',
	attrs     TEXT NOT NULL DEFAULT ''
);

-- ---------------------------------------------------------------------------
-- Kamailio db schema bookkeeping: several modules verify their table version
-- against this table at startup.
CREATE TABLE IF NOT EXISTS version (
	table_name    TEXT PRIMARY KEY,
	table_version INTEGER NOT NULL
);
INSERT OR REPLACE INTO version (table_name, table_version) VALUES
	('dispatcher', 4), ('dialplan', 2), ('trusted', 6), ('address', 6);

-- address: permissions module address groups (loaded because permissions is
-- in use; keep even if empty — usable for extra ACLs later).
CREATE TABLE IF NOT EXISTS address (
	id      INTEGER PRIMARY KEY AUTOINCREMENT,
	grp     INTEGER NOT NULL DEFAULT 0,
	ip_addr TEXT NOT NULL,
	mask    INTEGER NOT NULL DEFAULT 32,
	port    INTEGER NOT NULL DEFAULT 0,
	tag     TEXT
);

-- ---------------------------------------------------------------------------
-- trusted: permissions module table. One row per tenant voip-stack tunnel
-- address. tag = tenant domain -> allow_trusted() detects the ORIGIN tenant
-- by source address (the tunnel authenticates the peer), never by From.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS trusted (
	id           INTEGER PRIMARY KEY AUTOINCREMENT,
	src_ip       CHAR NOT NULL,
	proto        CHAR NOT NULL DEFAULT 'any',
	from_pattern CHAR,
	ruri_pattern CHAR,
	tag          CHAR,
	priority     INTEGER NOT NULL DEFAULT 0
);

-- ===========================================================================
-- TENANT DATA
--
-- Deployment overlay: Pangolin tunnels (hub/Gerbil on the VPS, Newt client in
-- each home) — see pangolin/SETUP.md. Domain convention:
--   sip.<client>.home.example.net
-- Per tenant: dispatcher set (10+N) = that home's voip-stack LAN IP:5060
-- (reached over native Newt routing; the client site must carry its LAN
-- subnet), set (30+N) = the same voip-stack for PSTN dial-out (the HT813
-- FXO stays LAN-only behind it). Pangolin "resources" / *.internal names
-- are NOT used anymore.
-- trusted.src_ip is the site's WIRE-OBSERVED native tunnel IP: pass it as
-- force-ip to pangolin/setup-trusted.sh (see pangolin/SETUP.md and
-- README.md §Onboarding step 2).
--
-- Template for the next client (copy & adjust):
--   INSERT OR REPLACE INTO tenant_registry (tprefix, tvalue) VALUES
--     ('sip.<c>.home.example.net', 'T<N>,<10+N>,<30+N>,<landline>,<label>,10,1+1+90');
--   INSERT OR REPLACE INTO tenant_landline (tprefix, tvalue) VALUES
--     ('<landline>', 'T<N>,<ringgroup>,<10+N>,1');
--   INSERT INTO dispatcher (setid, destination, description) VALUES
--     (<10+N>, 'sip:<voip-stack-lan-ip>:5060;transport=tcp', '<c> voip-stack'),
--     (<30+N>, 'sip:<voip-stack-lan-ip>:5060;transport=tcp', '<c> PSTN via voip-stack');
--   -- trusted row: INSERT via pangolin/setup-trusted.sh <c> (needs tunnel IP)
-- ===========================================================================

-- tenant alpha (client 1): landline 01111111, ring group "home".
-- Label = the landline itself (that's what the other end should see).
-- flags = ih_on+pstn_on[+block_dpid] -> 1+1 = inter-home + PSTN, no block partition
INSERT OR REPLACE INTO tenant_registry (tprefix, tvalue) VALUES
	('sip.alpha.home.example.net', 'T1,11,31,01111111,01111111,0,1+1');

INSERT OR REPLACE INTO tenant_landline (tprefix, tvalue) VALUES
	('01111111', 'T1,home,11,1');

-- HT813 is NOT exposed over the tunnel: the PSTN set points at voip-stack
-- itself, which onward-routes dial-out calls to its local HT813 trunk (the
-- standard voip-stack trunk config; the FXO port stays LAN-only).
--
-- Destination addressing convention (current): the voip-stack host's LAN IP,
-- reached through the site's native Newt routing (the Pangolin site carries
-- the home LAN subnet; no Pangolin "resources" / *.internal names involved).
INSERT INTO dispatcher (setid, destination, flags, priority, attrs, description)
VALUES
	(11, 'sip:10.0.0.10:5060;transport=tcp', 0, 0, '', 'alpha voip-stack (HA host LAN IP, native routing)'),
	(31, 'sip:10.0.0.10:5060;transport=tcp', 0, 0, '', 'alpha PSTN via voip-stack (HT813 trunk, LAN-only)');

-- No emergency or premium patterns are seeded: numbering is deployment-
-- specific.  Create them per country in the console's "Number rules" page
-- (dialplan CRUD), then point tenant_registry.dpid / block_dpid at them.

-- tenant beta (client 2): landline 02222222, ring group "home"
-- tenant gamma (client 3): landline 03333333, ring group "home"
INSERT OR REPLACE INTO tenant_registry (tprefix, tvalue) VALUES
	('sip.beta.home.example.net', 'T2,12,32,02222222,02222222,0,1+1'),
	('sip.gamma.home.example.net', 'T3,13,33,03333333,03333333,0,1+1');
INSERT OR REPLACE INTO tenant_landline (tprefix, tvalue) VALUES
	('02222222', 'T2,home,12,1'),
	('03333333', 'T3,home,13,1');
-- dispatcher destinations: each home's voip-stack LAN IP (native routing);
-- substitute the real values before seeding:
INSERT INTO dispatcher (setid, destination, flags, priority, attrs, description)
VALUES
	(12, 'sip:10.0.99.12:5060;transport=tcp', 0, 0, '', 'beta voip-stack (PLACEHOLDER — set its HA LAN IP)'),
	(32, 'sip:10.0.99.12:5060;transport=tcp', 0, 0, '', 'beta PSTN via voip-stack (PLACEHOLDER)'),
	(13, 'sip:10.0.99.13:5060;transport=tcp', 0, 0, '', 'gamma voip-stack (PLACEHOLDER — set its HA LAN IP)'),
	(33, 'sip:10.0.99.13:5060;transport=tcp', 0, 0, '', 'gamma PSTN via voip-stack (PLACEHOLDER)');

-- tenant detection by source: filled at deploy time (per home) by
-- pangolin/setup-trusted.sh <client> <db> <wire-observed-native-ip>:
-- INSERT INTO trusted (src_ip, proto, from_pattern, ruri_pattern, tag, priority)
-- VALUES ('<native-ip-of-site>', 'any', NULL, NULL,
--         'sip.<client>.home.example.net', 0);
