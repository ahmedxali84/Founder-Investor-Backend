"""
Runs before any test module is collected/imported.

Two real files must be redirected to throwaway temp paths BEFORE main_app
(or anything importing agents.store) is ever imported by a test:

1. agent_profiles.db — agents/store.py's init_db() runs unconditionally at
   import time.
2. sessions_db.json — several code paths that look like pure logic
   (_ensure_active_idea_id, various endpoint handlers) call save_sessions()
   as a side effect. A test that exercises one of these directly (not just
   through the HTTP layer) would otherwise silently overwrite the repo's
   real session data — this happened once already during this suite's own
   development: a test appended a fixture idea straight to GLOBAL_IDEAS_POOL,
   which _ensure_active_idea_id's save_sessions() call then wrote into the
   real sessions_db.json.
"""
import os
import tempfile

_tmp_agent_db = tempfile.NamedTemporaryFile(prefix="agent_profiles_test_", suffix=".db", delete=False)
_tmp_agent_db.close()
os.environ["AGENT_PROFILES_DB_PATH"] = _tmp_agent_db.name

_tmp_sessions_file = tempfile.NamedTemporaryFile(prefix="sessions_db_test_", suffix=".json", delete=False)
_tmp_sessions_file.close()
os.environ["SESSIONS_DB_FILE"] = _tmp_sessions_file.name
