-- Correct only the observed QMT month-series option grammar; preserve identities and job evidence.
UPDATE securities
SET kind='option', subtype='contract', updated_at=now()
WHERE source='qmt' AND kind='future'
  AND code ~ '^[A-Za-z_]+[0-9]{3,4}(-?MS)-?[CP]-?[0-9]+[.](ZF|DF)$';
