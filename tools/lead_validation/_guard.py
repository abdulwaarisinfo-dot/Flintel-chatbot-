"""
_guard.py — every staging-verification script calls `require_approval()` before it touches any real system.

The scripts in this folder are a TEST HARNESS: nothing here has been run against a real service. They refuse to do real I/O unless ALL of these hold:
  1. the flag `--approved-by "<owner name>"` is given (the owner's explicit approval, recorded in the report),
  2. the environment variable FLINTEL_STAGING_APPROVED is exactly "yes",
  3. FLINTEL_STAGING_ENV_NAME is set to a name that contains "staging" (never "prod"),
  4. for MongoDB: the database name used is a throw-away `flintel_leadtest_<random>` created by the script and dropped at the end; the script never
     reads or writes any other database.
Without them the script prints what it WOULD do (a dry run) and exits 0. Secrets are read from the environment only and never printed or written to a report.
"""
import os
import sys


class NotApproved(SystemExit):
    pass


def require_approval(approved_by, what: str, dry_run: bool):
    """Returns True when real I/O may happen. In a dry run, prints `what` and returns False. Raises SystemExit(2) when real I/O was requested but is not approved."""
    if dry_run:
        print(f"[DRY RUN] {what}\n[DRY RUN] nothing was contacted. Re-run with --execute and the approval conditions in tools/lead_validation/_guard.py.")
        return False
    problems = []
    if not approved_by or not str(approved_by).strip():
        problems.append('missing --approved-by "<owner name>"')
    if os.getenv("FLINTEL_STAGING_APPROVED") != "yes":
        problems.append('FLINTEL_STAGING_APPROVED is not "yes"')
    env_name = os.getenv("FLINTEL_STAGING_ENV_NAME", "")
    if "staging" not in env_name.lower() or "prod" in env_name.lower():
        problems.append('FLINTEL_STAGING_ENV_NAME must contain "staging" and must not contain "prod"')
    if problems:
        print("REFUSED: " + "; ".join(problems), file=sys.stderr)
        raise NotApproved(2)
    return True


def redact(text: str) -> str:
    """Removes anything that looks like credentials from a connection string before it is shown."""
    import re
    return re.sub(r"//[^/@]*@", "//***@", str(text))
