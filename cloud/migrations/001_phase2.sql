-- 控制面只保存身份、状态与对象 URI；大文件留在对象存储。
CREATE TABLE IF NOT EXISTS datasets (
  id VARCHAR(64) PRIMARY KEY,
  owner_id VARCHAR(64) NOT NULL,
  snapshot_uri VARCHAR(512) NOT NULL,
  created_at TIMESTAMP(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
  INDEX idx_datasets_owner (owner_id)
);

-- 一个 Job 对应一个 run；版本与 attempt 共同保护并发完成和重领。
CREATE TABLE IF NOT EXISTS jobs (
  id CHAR(32) PRIMARY KEY,
  dataset_id VARCHAR(64) NOT NULL,
  owner_id VARCHAR(64) NOT NULL,
  idempotency_key VARCHAR(128) NOT NULL,
  request_digest CHAR(64) NOT NULL,
  status ENUM('pending','running','succeeded','failed','cancel_requested') NOT NULL DEFAULT 'pending',
  status_version BIGINT UNSIGNED NOT NULL DEFAULT 0,
  attempts INT UNSIGNED NOT NULL DEFAULT 0,
  failure_type VARCHAR(32) NULL,
  input_manifest_uri VARCHAR(512) NOT NULL,
  output_manifest_uri VARCHAR(512) NULL,
  lease_until TIMESTAMP(6) NULL,
  created_at TIMESTAMP(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
  updated_at TIMESTAMP(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6),
  UNIQUE KEY uq_jobs_owner_idempotency (owner_id, idempotency_key),
  INDEX idx_jobs_claim (status, lease_until, created_at),
  CONSTRAINT fk_jobs_dataset FOREIGN KEY (dataset_id) REFERENCES datasets(id)
);

CREATE TABLE IF NOT EXISTS stages (
  job_id CHAR(32) NOT NULL,
  name VARCHAR(32) NOT NULL,
  status ENUM('pending','running','succeeded','failed','cancel_requested') NOT NULL,
  attempt_id VARCHAR(64) NULL,
  manifest_uri VARCHAR(512) NULL,
  failure_type VARCHAR(32) NULL,
  updated_at TIMESTAMP(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6),
  PRIMARY KEY (job_id, name),
  CONSTRAINT fk_stages_job FOREIGN KEY (job_id) REFERENCES jobs(id)
);

CREATE TABLE IF NOT EXISTS artifacts (
  job_id CHAR(32) NOT NULL,
  name VARCHAR(32) NOT NULL,
  attempt_id VARCHAR(64) NOT NULL,
  manifest_uri VARCHAR(512) NOT NULL,
  created_at TIMESTAMP(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
  PRIMARY KEY (job_id, name),
  CONSTRAINT fk_artifacts_job FOREIGN KEY (job_id) REFERENCES jobs(id)
);
