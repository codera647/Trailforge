// Contract: grant access only when admin is present.
export function isAdmin(roles: string[]): boolean {
  return Boolean(roles.indexOf("admin"));
}
