-- One row per source + logical request. Aggregate event and stage grains BEFORE joining.
CREATE TABLE IF NOT EXISTS warehouse_meta(version INTEGER PRIMARY KEY CHECK(version=1));
CREATE TABLE IF NOT EXISTS sources(
 source_id TEXT PRIMARY KEY, event_cursor INTEGER NOT NULL,
 prefix_hash TEXT NOT NULL, snapshot_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS fact_request(
 source_id TEXT NOT NULL REFERENCES sources(source_id), job_key TEXT NOT NULL,
 identity_hash TEXT NOT NULL, kind TEXT NOT NULL CHECK(kind IN ('api','pipeline')),
 stored_status TEXT NOT NULL, effective_status TEXT NOT NULL,
 attempts INTEGER, provider TEXT NOT NULL, mode TEXT NOT NULL,
 queued_at REAL, updated_at REAL NOT NULL, first_observed_at REAL,
 last_attempt_seconds REAL, row_hash TEXT NOT NULL,
 PRIMARY KEY(source_id,job_key)
);
CREATE TABLE IF NOT EXISTS fact_stage(
 source_id TEXT NOT NULL REFERENCES sources(source_id), job_key TEXT NOT NULL,
 stage_key TEXT NOT NULL, stage TEXT NOT NULL,
 committed_executions INTEGER NOT NULL CHECK(committed_executions>0), row_hash TEXT NOT NULL,
 PRIMARY KEY(source_id,job_key,stage_key)
);
CREATE TABLE IF NOT EXISTS fact_event(
 source_id TEXT NOT NULL REFERENCES sources(source_id), seq INTEGER NOT NULL,
 job_key TEXT, event TEXT NOT NULL, stage TEXT, at REAL NOT NULL,
 PRIMARY KEY(source_id,seq)
);
CREATE INDEX IF NOT EXISTS event_job ON fact_event(source_id,job_key);
CREATE VIEW IF NOT EXISTS request_metrics AS
WITH e AS (
 SELECT source_id,job_key,
 SUM(event='admitted') AS admitted_events,
 SUM(event='cache_hit') AS cache_hits,
 SUM(event='stage_started') AS stage_starts,
 SUM(event='stage_completed') AS stage_completion_events
 FROM fact_event GROUP BY source_id,job_key
), s AS (
 SELECT source_id,job_key,SUM(committed_executions) AS stage_commits,
 SUM(CASE WHEN stage='render' THEN committed_executions ELSE 0 END) AS render_commits
 FROM fact_stage GROUP BY source_id,job_key
)
SELECT r.*,COALESCE(e.admitted_events,0) AS admitted_events,
 COALESCE(e.cache_hits,0) AS cache_hits,COALESCE(e.stage_starts,0) AS stage_starts,
 COALESCE(e.stage_completion_events,0) AS stage_completion_events,
 COALESCE(s.stage_commits,0) AS stage_commits,COALESCE(s.render_commits,0) AS render_commits
FROM fact_request r LEFT JOIN e USING(source_id,job_key) LEFT JOIN s USING(source_id,job_key);

CREATE VIEW IF NOT EXISTS daily_metrics AS
SELECT date(first_observed_at,'unixepoch') AS cohort_day_utc,kind,provider,mode,
 COUNT(*) AS logical_requests,SUM(COALESCE(attempts,0)) AS api_attempts,
 SUM(effective_status='complete') AS complete,
 SUM(effective_status='failed') AS failed,
 SUM(effective_status='cancelled') AS cancelled,
 SUM(effective_status='timed_out') AS timed_out,
 SUM(effective_status='interrupted') AS interrupted,
 SUM(effective_status IN ('queued','running')) AS active,
 SUM(CASE WHEN stored_status IN ('queued','running') AND
   effective_status NOT IN ('queued','running') THEN 1 ELSE 0 END) AS inferred_terminal,
 SUM(stored_status NOT IN ('queued','running')) AS confirmed_terminal,
 SUM(CASE WHEN attempts>1 THEN 1 ELSE 0 END) AS resubmitted_requests,
 SUM(CASE WHEN attempts>1 AND effective_status='complete' THEN 1 ELSE 0 END)
   AS complete_after_resubmission,
 SUM(cache_hits) AS cache_hits,SUM(stage_starts) AS stage_starts,
 SUM(stage_completion_events) AS stage_completion_events,
 SUM(stage_commits) AS stage_commits,SUM(render_commits) AS render_commits
FROM request_metrics GROUP BY cohort_day_utc,kind,provider,mode;

-- Nearest-rank percentiles of confirmed terminal API attempts, not model/tool latency.
CREATE VIEW IF NOT EXISTS attempt_latency AS
WITH ranked AS (
 SELECT last_attempt_seconds,
 ROW_NUMBER() OVER (ORDER BY last_attempt_seconds,source_id,job_key) AS rn,
 COUNT(*) OVER () AS n
 FROM fact_request WHERE kind='api' AND last_attempt_seconds IS NOT NULL
)
SELECT COUNT(*) AS measured_requests,
 MAX(CASE WHEN rn=(n+1)/2 THEN last_attempt_seconds END) AS p50_seconds,
 MAX(CASE WHEN rn=(n*95+99)/100 THEN last_attempt_seconds END) AS p95_seconds
FROM ranked;
