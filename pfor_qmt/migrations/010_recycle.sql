ALTER TABLE datasets ADD COLUMN deleted_at timestamptz;
ALTER TABLE maintenance_plans ADD COLUMN deleted_at timestamptz;
ALTER TABLE jobs ADD COLUMN deleted_at timestamptz;
ALTER TABLE datasets ADD CONSTRAINT deleted_dataset_disabled CHECK (deleted_at IS NULL OR NOT scheduled);
ALTER TABLE maintenance_plans ADD CONSTRAINT deleted_plan_disabled CHECK (deleted_at IS NULL OR NOT enabled);
ALTER TABLE jobs ADD CONSTRAINT deleted_job_inactive CHECK (deleted_at IS NULL OR state NOT IN ('queued','running','retrying'));

-- Serialize enqueueing with recycling, including callers outside the console.
CREATE FUNCTION guard_recycled_job_scope() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
    removed timestamptz;
    linked record;
BEGIN
    IF NEW.kind IN ('download','catalog') AND NEW.state IN ('queued','running','retrying') THEN
        SELECT deleted_at INTO removed FROM datasets WHERE id::text=NEW.payload->>'dataset_id' FOR SHARE;
        IF removed IS NOT NULL THEN
            RAISE EXCEPTION 'Dataset is in recycle bin' USING ERRCODE='23514';
        END IF;
        FOR linked IN SELECT deleted_at FROM maintenance_plans
            WHERE id::text IN (NEW.payload->>'maintenance_id',NEW.payload->>'manual_maintenance_id') ORDER BY id FOR SHARE LOOP
            IF linked.deleted_at IS NOT NULL THEN
                RAISE EXCEPTION 'Maintenance plan is in recycle bin' USING ERRCODE='23514';
            END IF;
        END LOOP;
    END IF;
    RETURN NEW;
END;
$$;
CREATE TRIGGER jobs_recycled_scope BEFORE INSERT OR UPDATE OF state,payload ON jobs
    FOR EACH ROW EXECUTE FUNCTION guard_recycled_job_scope();
