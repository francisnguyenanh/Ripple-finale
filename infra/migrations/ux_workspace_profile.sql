-- UX: a workspace gets a readable name and description (additive, nullable: existing workspaces keep working and show their id).
ALTER TABLE v2_workspaces ADD COLUMN IF NOT EXISTS display_name TEXT;
ALTER TABLE v2_workspaces ADD COLUMN IF NOT EXISTS description TEXT;
ALTER TABLE v2_workspaces ADD COLUMN IF NOT EXISTS created_by TEXT;
