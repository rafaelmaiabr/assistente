-- 0022: escopo opcional por departamento para quem entra com Google (NULL = a empresa inteira).
ALTER TABLE auth_memberships ADD COLUMN departments TEXT CHECK (departments IS NULL OR json_valid(departments));
