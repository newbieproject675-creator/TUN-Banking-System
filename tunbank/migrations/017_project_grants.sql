-- 017: PROJECT_GRANT ( /grant project ) + the versioned project catalogue.
-- SQLite cannot widen a CHECK constraint in place, so the two disbursement tables are rebuilt with the extra kind exactly as
-- migration 007 rebuilt the ledger: rows are copied out, the children dropped first (foreign keys are enforced), both tables
-- recreated, rows copied back unchanged, then the same indexes and the same "outcome only" triggers recreated.

CREATE TABLE _m017_batches AS SELECT * FROM disbursement_batches;
CREATE TABLE _m017_items   AS SELECT * FROM disbursement_items;
DROP TABLE disbursement_items;
DROP TABLE disbursement_batches;

CREATE TABLE disbursement_batches (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at          TEXT NOT NULL,
    kind                TEXT NOT NULL CHECK (kind IN ('RAWS','WARCHEST_GRANT','CITY_GRANT','INFRA_GRANT','LAND_GRANT','PROJECT_GRANT')),
    actor               TEXT NOT NULL,
    funding_source      TEXT NOT NULL CHECK (funding_source IN ('ALLIANCE','MEMBER_AVAILABLE','MEMBER_LOCKED')),
    source_member_id    INTEGER,
    source_description  TEXT NOT NULL,
    paying_bank         TEXT,
    note                TEXT NOT NULL,
    parameters_json     TEXT NOT NULL,
    rules_version       TEXT,
    totals_json         TEXT NOT NULL,
    value_cents         INTEGER,
    price_snapshot_id   INTEGER,
    approval_id         INTEGER REFERENCES approval_requests(id),
    content_sha256      TEXT NOT NULL,
    request_key         TEXT NOT NULL UNIQUE,
    status              TEXT NOT NULL DEFAULT 'RUNNING' CHECK (status IN ('RUNNING','COMPLETED','PARTIAL','FAILED','HALTED')),
    finished_at         TEXT
);
INSERT INTO disbursement_batches(id,created_at,kind,actor,funding_source,source_member_id,source_description,paying_bank,note,parameters_json,
    rules_version,totals_json,value_cents,price_snapshot_id,approval_id,content_sha256,request_key,status,finished_at)
SELECT id,created_at,kind,actor,funding_source,source_member_id,source_description,paying_bank,note,parameters_json,
    rules_version,totals_json,value_cents,price_snapshot_id,approval_id,content_sha256,request_key,status,finished_at FROM _m017_batches;

CREATE TABLE disbursement_items (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id         INTEGER NOT NULL REFERENCES disbursement_batches(id),
    seq              INTEGER NOT NULL,
    nation_id        INTEGER NOT NULL,
    kind             TEXT NOT NULL CHECK (kind IN ('RAWS','WARCHEST_GRANT','CITY_GRANT','INFRA_GRANT','LAND_GRANT','PROJECT_GRANT')),
    amounts_json     TEXT NOT NULL,
    requirement_json TEXT,
    stock_json       TEXT,
    calculation_json TEXT NOT NULL,
    source_member_id INTEGER,
    lock_id          INTEGER,
    status           TEXT NOT NULL DEFAULT 'PENDING' CHECK (status IN ('PENDING','COMPLETED','FAILED','UNCERTAIN','REVIEW')),
    tx_id            INTEGER REFERENCES transactions(id),
    pnw_record_id    INTEGER,
    message          TEXT,
    UNIQUE (batch_id, seq)
);
INSERT INTO disbursement_items(id,batch_id,seq,nation_id,kind,amounts_json,requirement_json,stock_json,calculation_json,source_member_id,lock_id,
    status,tx_id,pnw_record_id,message)
SELECT id,batch_id,seq,nation_id,kind,amounts_json,requirement_json,stock_json,calculation_json,source_member_id,lock_id,
    status,tx_id,pnw_record_id,message FROM _m017_items;
DROP TABLE _m017_items;
DROP TABLE _m017_batches;

CREATE INDEX idx_disb_batches_sha ON disbursement_batches(content_sha256, created_at);
CREATE INDEX idx_disb_items_nation ON disbursement_items(nation_id);

CREATE TRIGGER trg_disb_items_frozen BEFORE UPDATE ON disbursement_items
WHEN NEW.batch_id != OLD.batch_id OR NEW.seq != OLD.seq OR NEW.nation_id != OLD.nation_id OR NEW.kind != OLD.kind
  OR NEW.amounts_json != OLD.amounts_json OR NEW.calculation_json != OLD.calculation_json
  OR IFNULL(NEW.requirement_json,'') != IFNULL(OLD.requirement_json,'') OR IFNULL(NEW.stock_json,'') != IFNULL(OLD.stock_json,'')
  OR IFNULL(NEW.source_member_id,0) != IFNULL(OLD.source_member_id,0) OR IFNULL(NEW.lock_id,0) != IFNULL(OLD.lock_id,0)
  OR (OLD.tx_id IS NOT NULL AND IFNULL(NEW.tx_id,0) != OLD.tx_id)
BEGIN SELECT RAISE(ABORT, 'a disbursement item can only change its outcome, never what was calculated'); END;
CREATE TRIGGER trg_disb_items_no_delete BEFORE DELETE ON disbursement_items
BEGIN SELECT RAISE(ABORT, 'disbursement items can never be deleted'); END;
CREATE TRIGGER trg_disb_batches_frozen BEFORE UPDATE ON disbursement_batches
WHEN NEW.kind != OLD.kind OR NEW.actor != OLD.actor OR NEW.funding_source != OLD.funding_source OR NEW.totals_json != OLD.totals_json
  OR NEW.parameters_json != OLD.parameters_json OR NEW.source_description != OLD.source_description OR NEW.note != OLD.note
  OR NEW.request_key != OLD.request_key OR NEW.content_sha256 != OLD.content_sha256 OR NEW.created_at != OLD.created_at
BEGIN SELECT RAISE(ABORT, 'a disbursement batch can only change its status'); END;
CREATE TRIGGER trg_disb_batches_no_delete BEFORE DELETE ON disbursement_batches
BEGIN SELECT RAISE(ABORT, 'disbursement batches can never be deleted'); END;

-- Every imported version of the project catalogue is kept forever; the newest one is the one in force.
CREATE TABLE project_catalogue_versions (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    imported_at   TEXT NOT NULL,
    imported_by   TEXT NOT NULL,
    source_name   TEXT NOT NULL,
    sha256        TEXT NOT NULL,
    project_count INTEGER NOT NULL,
    json_text     TEXT NOT NULL CHECK (length(json_text) > 2)
);
CREATE TRIGGER trg_project_catalogue_no_update BEFORE UPDATE ON project_catalogue_versions
BEGIN SELECT RAISE(ABORT, 'catalogue versions can never be changed'); END;
CREATE TRIGGER trg_project_catalogue_no_delete BEFORE DELETE ON project_catalogue_versions
BEGIN SELECT RAISE(ABORT, 'catalogue versions can never be deleted'); END;
