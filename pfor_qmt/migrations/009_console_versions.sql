ALTER TABLE datasets ADD COLUMN revision integer NOT NULL DEFAULT 1;
ALTER TABLE maintenance_plans ADD COLUMN revision integer NOT NULL DEFAULT 1;
CREATE FUNCTION bump_config_revision() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    NEW.revision := OLD.revision + CASE WHEN (to_jsonb(NEW) - ARRAY['revision','last_date','last_error','updated_at']) IS DISTINCT FROM (to_jsonb(OLD) - ARRAY['revision','last_date','last_error','updated_at']) THEN 1 ELSE 0 END;
    RETURN NEW;
END;
$$;
CREATE TRIGGER datasets_revision BEFORE UPDATE ON datasets FOR EACH ROW EXECUTE FUNCTION bump_config_revision();
CREATE TRIGGER maintenance_revision BEFORE UPDATE ON maintenance_plans FOR EACH ROW EXECUTE FUNCTION bump_config_revision();
CREATE INDEX jobs_console_source_created ON jobs ((coalesce(payload->>'source','qmt')),created_at DESC,id);
