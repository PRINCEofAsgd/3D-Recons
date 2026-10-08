-- Phase 4 与旧 worker 共存；默认值保留既有 Job 的执行语义。
ALTER TABLE jobs
  ADD COLUMN backend ENUM('worker','argo') NOT NULL DEFAULT 'worker',
  ADD COLUMN candidate VARCHAR(64) NOT NULL DEFAULT 'estimated',
  ADD COLUMN stitch_profile VARCHAR(32) NOT NULL DEFAULT 'default',
  ADD COLUMN image_ref VARCHAR(256) NULL,
  ADD COLUMN workflow_name VARCHAR(64) NULL;

-- blocked 是能力门禁的业务状态，不能记作 succeeded。
ALTER TABLE stages
  MODIFY COLUMN status ENUM('pending','running','succeeded','failed','blocked','cancel_requested') NOT NULL,
  ADD COLUMN failure_detail VARCHAR(1024) NULL;
